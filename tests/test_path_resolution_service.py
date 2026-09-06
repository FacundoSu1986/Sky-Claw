"""Tests para PathResolutionService — resolución stateless de rutas MO2/Skyrim.

Verifica EAFP anti-TOCTOU, validación con PathValidator (CRIT-003),
y la interfaz Protocol PathResolver.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import shutil
import tempfile
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from sky_claw.app.core.path_resolver import (
    PathResolutionService,
    PathResolver,
    resolver_mods_dir_de_instancia_mo2,
)
from sky_claw.app.security.path_validator import PathValidator
from tests._symlink_guard import symlink_guard

if TYPE_CHECKING:
    pass


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


def _formato_qt(path: pathlib.Path) -> str:
    """Formato de ruta de los INI de MO2 (Qt): separadores '/'."""
    return str(path).replace("\\", "/")


def _texto_ini_mo2(
    *,
    base_directory: str | None = None,
    mod_directory: str | None = None,
) -> str:
    """Texto de un ModOrganizer.ini realista: [Settings] plano + ruido Qt.

    El ruido (Geometry con @ByteArray) está a propósito: el parser del
    resolver solo debe leer [Settings] sin tropezar con el resto.
    """
    lineas = [
        "[General]",
        "gameName=Skyrim Special Edition",
        "selected_profile=@ByteArray(Default)",
        "",
        "[Settings]",
        "profile_local_inis=true",
    ]
    if base_directory is not None:
        lineas.append(f"base_directory={base_directory}")
    if mod_directory is not None:
        lineas.append(f"mod_directory={mod_directory}")
    lineas.append("")
    lineas.append("[Geometry]")
    lineas.append("MainWindow_state=@ByteArray(\\x1\\xd9\\x0\\x0\\xff)")
    return "\n".join(lineas) + "\n"


def _mismo_path(a: pathlib.Path, b: pathlib.Path) -> bool:
    """Comparación de paths insensible a case/separadores (Windows)."""
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


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
            "sky_claw.app.core.path_resolver._CANDIDATE_MO2_PATHS",
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
                "sky_claw.app.core.path_resolver._CANDIDATE_MO2_PATHS",
                (r"Z:\nonexistent\path",),
            ),
            patch(
                "sky_claw.app.core.path_resolver._CANDIDATE_PF_PATHS",
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
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(path_resolver, "detect_mo2_path", return_value=None),
            pytest.raises(RuntimeError, match="No se pudo resolver"),
        ):
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
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(path_resolver, "detect_mo2_path", return_value=None),
            pytest.raises(RuntimeError, match="No se pudo detectar"),
        ):
            path_resolver.get_mo2_mods_path()


class TestGetActiveProfile:
    """Tests para get_active_profile."""

    def test_returns_env_var_profile(self, sandbox_root: pathlib.Path) -> None:
        """Sin perfil inyectado, MO2_PROFILE decide."""
        validator = PathValidator(roots=[sandbox_root])
        resolver = PathResolutionService(path_validator=validator)
        with patch.dict(os.environ, {"MO2_PROFILE": "CustomProfile"}):
            assert resolver.get_active_profile() == "CustomProfile"

    def test_returns_constructor_profile_when_no_env(
        self,
        path_resolver: PathResolutionService,
    ) -> None:
        """Retorna perfil del constructor si no hay env var."""
        with patch.dict(os.environ, {}, clear=True):
            assert path_resolver.get_active_profile() == "TestProfile"

    def test_el_perfil_inyectado_gana_sobre_la_env_var(
        self,
        path_resolver: PathResolutionService,
    ) -> None:
        """Este test afirmaba lo CONTRARIO y congelaba el defecto.

        Con la precedencia vieja (entorno primero), un `--profile Requiem` junto a
        un `MO2_PROFILE=Default` hacía que `AppContext` resolviera `Requiem` para
        las tools del agente y este resolver devolviera `Default` para LOOT,
        DynDOLOD, Pandora, Wrye Bash y Synthesis: la divergencia GUI↔agente
        sobrevivía a que el perfil se inyectara bien. Quien inyecta ya consultó
        `MO2_PROFILE` con la precedencia correcta, así que volver a leerlo acá no
        agrega una fuente — pisa una decisión ya tomada.
        """
        with patch.dict(os.environ, {"MO2_PROFILE": "PerfilDelEntorno"}):
            assert path_resolver.get_active_profile() == "TestProfile"

    def test_returns_default_when_nothing_set(self, sandbox_root: pathlib.Path) -> None:
        """Retorna 'Default' cuando no hay perfil configurado."""
        validator = PathValidator(roots=[sandbox_root])
        resolver = PathResolutionService(path_validator=validator)
        with patch.dict(os.environ, {}, clear=True):
            assert resolver.get_active_profile() == "Default"

    def test_el_perfil_vacio_cuenta_como_ausencia(self, sandbox_root: pathlib.Path) -> None:
        """`""` no es un perfil: cae al entorno y después al fallback.

        `--profile` declara `default=""` en `__main__.py`, así que un caller que
        enhebre el valor crudo del CLI inyecta la cadena vacía. Evaluando por
        `is None` eso devolvía `""` y los runners (LOOT, Synthesis) armaban rutas
        y líneas de comando con un perfil inexistente en vez del fallback. La
        prueba por truthiness es además la MISMA que hace
        `AppContext._resolve_mo2_profile`: si las dos no coinciden, vuelve la
        divergencia que este resolver cierra (hallazgo de review de Qodo, #460).
        """
        validator = PathValidator(roots=[sandbox_root])
        resolver = PathResolutionService(path_validator=validator, profile_name="")

        with patch.dict(os.environ, {}, clear=True):
            assert resolver.get_active_profile() == "Default"
        with patch.dict(os.environ, {"MO2_PROFILE": "PerfilDelEntorno"}):
            assert resolver.get_active_profile() == "PerfilDelEntorno"

    def test_la_precedencia_coincide_con_la_de_app_context(self, sandbox_root: pathlib.Path) -> None:
        """Las dos resoluciones del perfil tienen que dar lo mismo ante la misma
        entrada. Son piezas distintas —`AppContext` para las tools del agente, este
        resolver para los runners de la GUI— y su desacuerdo ES el defecto."""
        from types import SimpleNamespace

        from sky_claw.app_context import _resolve_mo2_profile

        validator = PathValidator(roots=[sandbox_root])
        casos = [("Requiem", "Entorno"), ("", "Entorno"), ("Requiem", ""), ("", "")]

        for cli, entorno in casos:
            entorno_parcheado = {"MO2_PROFILE": entorno} if entorno else {}
            with patch.dict(os.environ, entorno_parcheado, clear=True):
                desde_app_context = _resolve_mo2_profile(SimpleNamespace(profile=cli))
                desde_resolver = PathResolutionService(path_validator=validator, profile_name=cli).get_active_profile()

            assert desde_app_context == desde_resolver, f"divergen con cli={cli!r} entorno={entorno!r}"


class TestResolverModsDirDeInstanciaMo2:
    """Unit tests de la función pura del contrato PathSettings de MO2.

    La semántica está verificada contra el código fuente de MO2
    (src/settings.cpp, PathSettings::base/mods/resolve): mod_directory manda;
    si falta, base_directory/mods; si base_directory falta, el directorio del
    propio INI; el literal %BASE_DIR% se sustituye por base().
    """

    def test_sin_claves_usa_el_directorio_del_ini(self, tmp_path: pathlib.Path) -> None:
        """Portable puro: sin base_directory ni mod_directory → <ini_dir>/mods."""
        resultado = resolver_mods_dir_de_instancia_mo2(
            _texto_ini_mo2(),
            tmp_path,
        )
        assert resultado is not None
        assert _mismo_path(resultado, tmp_path / "mods")

    def test_base_directory_absoluta_manda_sobre_el_directorio_del_ini(self, tmp_path: pathlib.Path) -> None:
        """Instancia global: base_directory declara los datos en otro árbol."""
        base = tmp_path / "Modding" / "MO2" / "SkyrimSE"
        resultado = resolver_mods_dir_de_instancia_mo2(
            _texto_ini_mo2(base_directory=_formato_qt(base)),
            tmp_path / "LocalAppData" / "ModOrganizer" / "SkyrimSE",
        )
        assert resultado is not None
        assert _mismo_path(resultado, base / "mods")

    def test_mod_directory_explicito_manda_sobre_base_directory(self, tmp_path: pathlib.Path) -> None:
        """mod_directory personalizado (absoluto) gana sobre base/mods."""
        base = tmp_path / "base"
        mods_personalizados = tmp_path / "ModsPersonales"
        resultado = resolver_mods_dir_de_instancia_mo2(
            _texto_ini_mo2(
                base_directory=_formato_qt(base),
                mod_directory=_formato_qt(mods_personalizados),
            ),
            tmp_path,
        )
        assert resultado is not None
        assert _mismo_path(resultado, mods_personalizados)

    def test_mod_directory_con_base_dir_se_expande(self, tmp_path: pathlib.Path) -> None:
        """%BASE_DIR% se sustituye por base_directory (PathSettings::resolve)."""
        base = tmp_path / "base"
        resultado = resolver_mods_dir_de_instancia_mo2(
            _texto_ini_mo2(
                base_directory=_formato_qt(base),
                mod_directory="%BASE_DIR%/ModsPersonalizados",
            ),
            tmp_path,
        )
        assert resultado is not None
        assert _mismo_path(resultado, base / "ModsPersonalizados")

    def test_valor_relativo_es_fail_closed(self, tmp_path: pathlib.Path) -> None:
        """Un mod_directory relativo no se resuelve contra el cwd: None."""
        assert (
            resolver_mods_dir_de_instancia_mo2(
                _texto_ini_mo2(
                    base_directory=_formato_qt(tmp_path / "base"),
                    mod_directory="mis_mods",
                ),
                tmp_path,
            )
            is None
        )

    def test_el_ruido_qt_de_otras_secciones_no_rompe_el_parseo(self, tmp_path: pathlib.Path) -> None:
        """@ByteArray, claves exóticas y BOM fuera de [Settings] se ignoran."""
        texto = "\ufeff[General]\ngameName=X\n" + _texto_ini_mo2(base_directory=_formato_qt(tmp_path))
        resultado = resolver_mods_dir_de_instancia_mo2(texto, tmp_path)
        assert resultado is not None
        assert _mismo_path(resultado, tmp_path / "mods")


class TestGetMo2ModsPathDeInstancia:
    """get_mo2_mods_path resuelve mods desde la metadata de la instancia MO2.

    Cubre la clase de defecto "instalación del programa != datos de la
    instancia": el directorio de ModOrganizer.exe ya no implica <exe>/mods
    cuando la instancia declara su base en otro disco/árbol.
    """

    def _montar_instancia_global(
        self,
        tmp_path: pathlib.Path,
        *,
        nombre_instancia: str = "SkyrimSE",
    ) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
        """Arma exe_dir + LOCALAPPDATA con una instancia global bajo tmp_path.

        Returns:
            (exe_dir, dir_de_instancia, mods_dir, local_app_data)
        """
        exe_dir = tmp_path / "Modding" / "ModOrganizer2"
        exe_dir.mkdir(parents=True)
        (exe_dir / "ModOrganizer.exe").write_bytes(b"fake exe")
        dir_de_instancia = tmp_path / "Modding" / "MO2" / "SkyrimSE"
        mods_dir = dir_de_instancia / "mods"
        mods_dir.mkdir(parents=True)
        local_app_data = tmp_path / "LocalAppData"
        ini_dir = local_app_data / "ModOrganizer" / nombre_instancia
        ini_dir.mkdir(parents=True)
        (ini_dir / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(base_directory=_formato_qt(dir_de_instancia)),
            encoding="utf-8",
        )
        return exe_dir, dir_de_instancia, mods_dir, local_app_data

    def test_t1_instancia_global_separada_no_usa_exe_mods(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """Caso A: exe dir != base_directory → mods desde la instancia.

        Se crea además un <exe>/mods como trampa: la implementación vieja lo
        elegía (MO2_PATH/mods) y esta debe ignorarlo.
        """
        exe_dir, _dir_de_instancia, mods_dir, local_app_data = self._montar_instancia_global(tmp_path)
        (exe_dir / "mods").mkdir()  # trampa: mods colgando del exe

        with patch.dict(
            os.environ,
            {"MO2_PATH": str(exe_dir), "LOCALAPPDATA": str(local_app_data)},
            clear=True,
        ):
            resultado = path_resolver.get_mo2_mods_path()

        assert _mismo_path(resultado, mods_dir)

    def test_t1b_instancia_global_sin_mo2_path_ni_deteccion(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """La instancia global se descubre sin conocer la instalación del exe."""
        _exe_dir, _dir_de_instancia, mods_dir, local_app_data = self._montar_instancia_global(tmp_path)

        with (
            patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}, clear=True),
            patch.object(path_resolver, "detect_mo2_path", return_value=None),
        ):
            resultado = path_resolver.get_mo2_mods_path()

        assert _mismo_path(resultado, mods_dir)

    def test_t2_portable_sigue_funcionando(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """Caso B: INI junto al exe sin base_directory → <exe>/mods."""
        exe_dir = tmp_path / "MO2Portable"
        exe_dir.mkdir(parents=True)
        (exe_dir / "ModOrganizer.exe").write_bytes(b"fake exe")
        (exe_dir / "mods").mkdir()
        (exe_dir / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(),  # sin base_directory: base = dir del INI
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"MO2_PATH": str(exe_dir)}, clear=True):
            resultado = path_resolver.get_mo2_mods_path()

        assert _mismo_path(resultado, exe_dir / "mods")

    def test_t3_mod_directory_explicito(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """T3: mod_directory personalizado manda sobre base/mods."""
        base = tmp_path / "base"
        mods_personalizados = tmp_path / "ModsPersonales"
        mods_personalizados.mkdir(parents=True)
        ini_dir = tmp_path / "LocalAppData" / "ModOrganizer" / "SkyrimSE"
        ini_dir.mkdir(parents=True)
        (ini_dir / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(
                base_directory=_formato_qt(base),
                mod_directory=_formato_qt(mods_personalizados),
            ),
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {"LOCALAPPDATA": str(tmp_path / "LocalAppData")}, clear=True),
            patch.object(path_resolver, "detect_mo2_path", return_value=None),
        ):
            resultado = path_resolver.get_mo2_mods_path()

        assert _mismo_path(resultado, mods_personalizados)

    def test_t3b_mod_directory_con_base_dir_sobre_instancia_detectada(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """%BASE_DIR% se expande contra base_directory de la instancia."""
        base = tmp_path / "base"
        mods_personalizados = base / "ModsPersonalizados"
        mods_personalizados.mkdir(parents=True)
        exe_dir = tmp_path / "exe"
        exe_dir.mkdir(parents=True)
        (exe_dir / "ModOrganizer.exe").write_bytes(b"fake exe")
        ini_dir = tmp_path / "LocalAppData" / "ModOrganizer" / "SkyrimSE"
        ini_dir.mkdir(parents=True)
        (ini_dir / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(
                base_directory=_formato_qt(base),
                mod_directory="%BASE_DIR%/ModsPersonalizados",
            ),
            encoding="utf-8",
        )

        with patch.dict(
            os.environ,
            {"MO2_PATH": str(exe_dir), "LOCALAPPDATA": str(tmp_path / "LocalAppData")},
            clear=True,
        ):
            resultado = path_resolver.get_mo2_mods_path()

        assert _mismo_path(resultado, mods_personalizados)

    def test_t4_mo2_mods_path_override_manda_sobre_metadata(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """Caso C: MO2_MODS_PATH explícito gana aunque la instancia diga otra cosa."""
        exe_dir, _dir_de_instancia, _mods_dir, local_app_data = self._montar_instancia_global(tmp_path)
        override = tmp_path / "override_mods"
        override.mkdir()

        with patch.dict(
            os.environ,
            {
                "MO2_PATH": str(exe_dir),
                "MO2_MODS_PATH": str(override),
                "LOCALAPPDATA": str(local_app_data),
            },
            clear=True,
        ):
            resultado = path_resolver.get_mo2_mods_path()

        assert _mismo_path(resultado, override)

    def test_t5_sin_metadata_ni_legacy_falla_cerrado(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """Caso D: sin INI de instancia ni MO2_PATH/mods → RuntimeError."""
        exe_dir = tmp_path / "exe"
        exe_dir.mkdir(parents=True)
        (exe_dir / "ModOrganizer.exe").write_bytes(b"fake exe")

        with (
            patch.dict(os.environ, {"MO2_PATH": str(exe_dir)}, clear=True),
            patch.object(path_resolver, "detect_mo2_path", return_value=None),
            pytest.raises(RuntimeError, match="No se pudo detectar"),
        ):
            path_resolver.get_mo2_mods_path()

    def test_t5b_base_inexistente_no_degrada_a_exe_mods(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """Caso D: base_directory inexistente → fail-closed, aunque <exe>/mods exista.

        La metadata afirma otra ubicación; elegir <exe>/mods sería la
        invención silenciosa que este PR elimina.
        """
        exe_dir, _dir_de_instancia, _mods_dir, local_app_data = self._montar_instancia_global(tmp_path)
        # La instancia declara una base que NO existe...
        ini_path = local_app_data / "ModOrganizer" / "SkyrimSE" / "ModOrganizer.ini"
        ini_path.write_text(
            _texto_ini_mo2(base_directory=_formato_qt(tmp_path / "base_inexistente")),
            encoding="utf-8",
        )
        # ...y el exe sí tiene un mods/ (trampa para la degradación).
        (exe_dir / "mods").mkdir()

        with (
            patch.dict(
                os.environ,
                {"MO2_PATH": str(exe_dir), "LOCALAPPDATA": str(local_app_data)},
                clear=True,
            ),
            pytest.raises(RuntimeError, match="declara su directorio de mods"),
        ):
            path_resolver.get_mo2_mods_path()

    def test_t5c_ini_ilegible_falla_cerrado(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """Caso D: INI presente pero ilegible → RuntimeError con evidencia."""
        exe_dir = tmp_path / "exe"
        exe_dir.mkdir(parents=True)
        (exe_dir / "ModOrganizer.exe").write_bytes(b"fake exe")
        ini_path = exe_dir / "ModOrganizer.ini"
        ini_path.write_text(_texto_ini_mo2(), encoding="utf-8")

        lectura_original = pathlib.Path.read_text

        def _lectura_que_falla(self: pathlib.Path, *args: object, **kwargs: object) -> str:
            if self == ini_path:
                raise OSError("simulado")
            return lectura_original(self, *args, **kwargs)

        with (
            patch.dict(os.environ, {"MO2_PATH": str(exe_dir)}, clear=True),
            patch.object(pathlib.Path, "read_text", _lectura_que_falla),
            pytest.raises(RuntimeError, match="No se pudo leer el ModOrganizer.ini"),
        ):
            path_resolver.get_mo2_mods_path()

    def test_t5d_varias_instancias_globales_falla_cerrado(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """Varias instancias globales sin criterio → fail-closed con nombres."""
        exe_dir = tmp_path / "exe"
        exe_dir.mkdir(parents=True)
        (exe_dir / "ModOrganizer.exe").write_bytes(b"fake exe")
        local_app_data = tmp_path / "LocalAppData"
        for nombre in ("SkyrimSE", "Requiem"):
            ini_dir = local_app_data / "ModOrganizer" / nombre
            ini_dir.mkdir(parents=True)
            (ini_dir / "ModOrganizer.ini").write_text(
                _texto_ini_mo2(base_directory=_formato_qt(tmp_path / nombre)),
                encoding="utf-8",
            )

        with (
            patch.dict(
                os.environ,
                {"MO2_PATH": str(exe_dir), "LOCALAPPDATA": str(local_app_data)},
                clear=True,
            ),
            pytest.raises(RuntimeError, match="instancias globales de MO2"),
        ):
            path_resolver.get_mo2_mods_path()

    def test_t6_metadata_fuera_del_sandbox_falla_cerrado(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """T6: path declarado por la instancia fuera de las raíces → fail-closed."""
        exe_dir = tmp_path / "exe"
        exe_dir.mkdir(parents=True)
        (exe_dir / "ModOrganizer.exe").write_bytes(b"fake exe")
        # Sandbox acotado al exe: la base de la instancia queda fuera.
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[exe_dir]),
            profile_name="TestProfile",
        )
        dir_de_instancia = tmp_path / "instancia"
        mods_dir = dir_de_instancia / "mods"
        mods_dir.mkdir(parents=True)
        local_app_data = tmp_path / "LocalAppData"
        ini_dir = local_app_data / "ModOrganizer" / "SkyrimSE"
        ini_dir.mkdir(parents=True)
        (ini_dir / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(base_directory=_formato_qt(dir_de_instancia)),
            encoding="utf-8",
        )

        with (
            patch.dict(
                os.environ,
                {"MO2_PATH": str(exe_dir), "LOCALAPPDATA": str(local_app_data)},
                clear=True,
            ),
            pytest.raises(RuntimeError, match="fuera de las raíces permitidas"),
        ):
            resolver.get_mo2_mods_path()

    def test_t6b_metadata_con_traversal_falla_cerrado(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """T6: '..' en base_directory → PathValidator lo rechaza (CRIT-003)."""
        exe_dir = tmp_path / "exe"
        exe_dir.mkdir(parents=True)
        (exe_dir / "ModOrganizer.exe").write_bytes(b"fake exe")
        local_app_data = tmp_path / "LocalAppData"
        ini_dir = local_app_data / "ModOrganizer" / "SkyrimSE"
        ini_dir.mkdir(parents=True)
        (ini_dir / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(base_directory=_formato_qt(tmp_path) + "/../escape"),
            encoding="utf-8",
        )

        with (
            patch.dict(
                os.environ,
                {"MO2_PATH": str(exe_dir), "LOCALAPPDATA": str(local_app_data)},
                clear=True,
            ),
            pytest.raises(RuntimeError, match="fuera de las raíces permitidas"),
        ):
            path_resolver.get_mo2_mods_path()

    def test_scan_de_instancias_globales_con_error_falla_cerrado(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """Un OSError al escanear la raíz de instancias NO degrada al legacy.

        La raíz existe pero el scan aborta: hay metadata potencial inaccesible y
        degradar a <exe>/mods sería la invención silenciosa que el PR elimina.
        """
        exe_dir = tmp_path / "exe"
        exe_dir.mkdir(parents=True)
        (exe_dir / "ModOrganizer.exe").write_bytes(b"fake exe")
        (exe_dir / "mods").mkdir()  # trampa: si degradara, la elegiría
        local_app_data = tmp_path / "LocalAppData"
        raiz_instancias = local_app_data / "ModOrganizer"
        raiz_instancias.mkdir(parents=True)

        iteracion_original = pathlib.Path.iterdir

        def _iteracion_que_falla(self: pathlib.Path, *args: object, **kwargs: object):
            if self == raiz_instancias:
                raise OSError("simulado")
            return iteracion_original(self, *args, **kwargs)

        with (
            patch.dict(
                os.environ,
                {"MO2_PATH": str(exe_dir), "LOCALAPPDATA": str(local_app_data)},
                clear=True,
            ),
            patch.object(pathlib.Path, "iterdir", _iteracion_que_falla),
            pytest.raises(RuntimeError, match="No se pudo escanear"),
        ):
            path_resolver.get_mo2_mods_path()

    def test_multi_instancia_con_mo2_path_de_datos_deferre_al_legacy(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """MO2_PATH explícito apuntando a datos (sin exe) no falla con varias
        instancias globales: la config del operador que funcionaba vía
        <MO2_PATH>/mods sigue funcionando."""
        datos = tmp_path / "datos"
        mods_de_datos = datos / "mods"
        mods_de_datos.mkdir(parents=True)
        local_app_data = tmp_path / "LocalAppData"
        for nombre in ("SkyrimSE", "Requiem"):
            ini_dir = local_app_data / "ModOrganizer" / nombre
            ini_dir.mkdir(parents=True)
            (ini_dir / "ModOrganizer.ini").write_text(
                _texto_ini_mo2(base_directory=_formato_qt(tmp_path / nombre)),
                encoding="utf-8",
            )

        with patch.dict(
            os.environ,
            {"MO2_PATH": str(datos), "LOCALAPPDATA": str(local_app_data)},
            clear=True,
        ):
            resultado = path_resolver.get_mo2_mods_path()

        assert _mismo_path(resultado, mods_de_datos)

    def test_mo2_path_de_datos_no_lo_preempta_una_instancia_ajena(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """Un contenedor legacy declarado con MO2_PATH (mods/ sin INI) no es
        preemptado por una instancia global no relacionada."""
        datos = tmp_path / "contenedor_legacy"
        mods_de_datos = datos / "mods"
        mods_de_datos.mkdir(parents=True)
        # Instancia global única cuyo base_directory es OTRO directorio.
        _exe_dir, dir_de_instancia, _mods_dir, local_app_data = self._montar_instancia_global(tmp_path)

        with patch.dict(
            os.environ,
            {"MO2_PATH": str(datos), "LOCALAPPDATA": str(local_app_data)},
            clear=True,
        ):
            resultado = path_resolver.get_mo2_mods_path()

        assert _mismo_path(resultado, mods_de_datos)
        assert not _mismo_path(resultado, dir_de_instancia / "mods")

    def test_t7_regresion_exacta_del_bug_exe_dir_vs_base_directory(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """Regresión exacta del bug del rig: C:\\Modding\\ModOrganizer2 (exe) vs
        G:\\Modding\\MO2\\SkyrimSE (base_directory), modelado bajo tmp_path.

        Con la implementación anterior este fixture terminaba en RuntimeError
        (MO2_PATH/mods inexistente y auto-detección sin candidatos). Con la
        nueva resuelve el mods de la instancia.
        """
        exe_dir, dir_de_instancia, mods_dir, local_app_data = self._montar_instancia_global(tmp_path)

        # La implementación vieja dependía de la auto-detección tras fallar
        # MO2_PATH/mods: se congela a None para que el fallo sea determinista
        # (y el test no dependa del rig donde corre).
        with (
            patch.dict(
                os.environ,
                {"MO2_PATH": str(exe_dir), "LOCALAPPDATA": str(local_app_data)},
                clear=True,
            ),
            patch.object(path_resolver, "detect_mo2_path", return_value=None),
        ):
            resultado = path_resolver.get_mo2_mods_path()

        assert _mismo_path(resultado, dir_de_instancia / "mods")
        assert mods_dir.is_dir()  # evidencia física del fixture


class TestGuardiaTestsHermeticos:
    """El archivo no puede escribir fuera de tmp_path ni tocar el rig real.

    Guardia por AST: ninguna operación mutante del filesystem (mkdir,
    write_text, touch, open, replace, ...) puede recibir como argumento un
    literal de ruta absoluta (drive de Windows o UNC) — ni en sus args, ni
    en el receiver. Un test que construya ``pathlib.Path("C:\\\\x").mkdir()``
    o ``pathlib.Path(tmp)/"sub"/open("C:\\\\x", "w")`` viola el contrato. Los
    fixtures escriben bajo ``tmp_path`` (variable, no literal); un test
    nuevo que cree accidentalmente ``<volumen>\\Sky-Claw`` o toque la
    instancia MO2 del operador rompe acá. Literales de drive en asserts o
    patches (solo lectura) siguen permitidos.
    """

    _DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
    _UNC_RE = re.compile(r"^\\\\")
    _MUTADORES = {
        "mkdir",
        "write_text",
        "write_bytes",
        "touch",
        "unlink",
        "replace",
        "rename",
        "rmdir",
        "symlink_to",
        "hardlink_to",
        "mkstemp",
        "makedirs",
        "rmtree",
        "copytree",
        "copy",
        "copy2",
        "open",
    }

    @staticmethod
    def _violaciones_de_mutacion(arbol: ast.AST) -> list[str]:
        """Operaciones mutantes que reciben un literal de ruta absoluta.

        Recorre cada ``Call`` cuyo ``func`` es un mutante y, además de mirar
        sus argumentos directos, baja por la expresión del ``receiver``
        (``func.value`` cuando ``func`` es ``Attribute``) para detectar
        ``pathlib.Path("C:\\\\x").mkdir()`` y similares. Devuelve la lista de
        violaciones en formato ``"<mutante>(...<literal>...)"``.
        """
        violaciones: list[str] = []
        drive_re = TestGuardiaTestsHermeticos._DRIVE_RE
        unc_re = TestGuardiaTestsHermeticos._UNC_RE
        mutadores = TestGuardiaTestsHermeticos._MUTADORES

        def _buscar_literales(node: ast.AST | None) -> list[str]:
            """Drena un sub-árbol en busca de literales de drive/UNC."""
            encontrados: list[str] = []
            for sub in ast.walk(node) if node is not None else ():
                if (
                    isinstance(sub, ast.Constant)
                    and isinstance(sub.value, str)
                    and (drive_re.match(sub.value) or unc_re.match(sub.value))
                ):
                    encontrados.append(sub.value)
            return encontrados

        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            func = nodo.func
            nombre_mutante: str | None = None
            if isinstance(func, ast.Attribute) and func.attr in mutadores:
                nombre_mutante = func.attr
            elif isinstance(func, ast.Name) and func.id in mutadores:
                nombre_mutante = func.id
            if nombre_mutante is None:
                continue
            # 1) Literales en args/keyword path del propio mutante.
            args = list(nodo.args) + [kw.value for kw in nodo.keywords if kw.arg == "path"]
            for arg in args:
                encontrados = _buscar_literales(arg)
                for lit in encontrados:
                    violaciones.append(f"{nombre_mutante}(...{lit!r}...)")
            # 2) Literales en el receiver (e.g. ``pathlib.Path("C:\\x").mkdir()``).
            if isinstance(func, ast.Attribute):
                receiver_lits = _buscar_literales(func.value)
                for lit in receiver_lits:
                    violaciones.append(f"<receiver:{lit!r}>.{nombre_mutante}()")
        return violaciones

    def test_no_hay_literales_de_drive_en_operaciones_mutantes(self) -> None:
        """Las escrituras nunca reciben rutas absolutas literales del operador."""
        texto = pathlib.Path(__file__).read_text(encoding="utf-8")
        violaciones = self._violaciones_de_mutacion(ast.parse(texto))
        assert violaciones == [], (
            f"Operaciones mutantes con ruta absoluta literal: {violaciones}. Los tests solo escriben bajo tmp_path."
        )

    def test_los_docstrings_citan_el_rig_como_evidencia(self) -> None:
        """El layout real del bug se nombra solo como evidencia documental."""
        texto = pathlib.Path(__file__).read_text(encoding="utf-8")
        # En el fuente los backslashes van escapados (C:\\Modding\\...).
        assert "C:\\\\Modding\\\\ModOrganizer2" in texto
        assert "G:\\\\Modding\\\\MO2\\\\SkyrimSE" in texto

    # Unit tests del detector con snippets aislados (herméticos: el snippet
    # vive dentro de ``ast.parse(...)``, no como código de test ejecutable).
    def test_detector_tacha_path_receiver_mutante(self) -> None:
        """``pathlib.Path("C:\\\\x").mkdir()`` se detecta vía el receiver."""
        # ``"C:\\\\x"`` aparece aquí solo como argumento de ast.parse: no es
        # una mutación de filesystem en sí (la string se evalúa y se
        # descarta). El detector debe marcarlo igual porque la INTENCIÓN del
        # patrón es flaggear mutaciones con rutas absolutas.
        fuente = 'import pathlib\npathlib.Path("C:\\\\x").mkdir()\n'
        violaciones = self._violaciones_de_mutacion(ast.parse(fuente))
        assert any("receiver" in v or r"C:\\x" in v for v in violaciones)

    def test_detector_permite_path_receiver_con_variable(self) -> None:
        """``pathlib.Path(tmp_path).mkdir()`` con variable NO se tacha."""
        fuente = "pathlib.Path(tmp_path).mkdir(parents=True)\n"
        violaciones = self._violaciones_de_mutacion(ast.parse(fuente))
        assert violaciones == []

    def test_detector_tacha_open_con_drive_literales(self) -> None:
        """``open("C:\\\\x", "w")`` se detecta (mutante por nombre)."""
        fuente = 'open("C:\\\\x", "w")\n'
        violaciones = self._violaciones_de_mutacion(ast.parse(fuente))
        assert any("open" in v and r"C:\\x" in v for v in violaciones)

    def test_detector_permite_open_con_variable(self) -> None:
        """``open(str(tmp_path/"x"), "w")`` NO se tacha."""
        fuente = 'open(str(tmp_path / "x"), "w")\n'
        violaciones = self._violaciones_de_mutacion(ast.parse(fuente))
        assert violaciones == []


class TestModsPathEsDirectorio:
    """get_mo2_mods_path rechaza paths que existen pero no son directorios.

    Cobertura del contrato: un ``ModOrganizer.ini`` (o un override
    ``MO2_MODS_PATH``) que apunta a un archivo, no a un directorio, es
    fail-closed. Sin este control, el resolver devolvería el path y los
    callers fallarían luego con errores crípticos al listar/iterar el
    directorio de mods.
    """

    def test_metadata_de_instancia_apuntando_a_archivo_falla_cerrado(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """``base_directory/mods`` resuelve a un archivo, no a un directorio."""
        base = tmp_path / "instance"
        base.mkdir()
        # Crear un archivo en el lugar donde el resolver espera el directorio.
        (base / "mods").write_text("no es un directorio", encoding="utf-8")
        # INI portable junto al exe para que la metadata se descubra por la
        # ruta portable (sin tocar LOCALAPPDATA real de la máquina).
        exe_dir = tmp_path / "exe"
        exe_dir.mkdir()
        (exe_dir / "ModOrganizer.exe").write_bytes(b"fake")
        (exe_dir / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(base_directory=_formato_qt(base)),
            encoding="utf-8",
        )

        with (
            patch.dict(
                os.environ,
                {"MO2_PATH": str(exe_dir), "LOCALAPPDATA": str(tmp_path / "no_lappdata")},
                clear=True,
            ),
            pytest.raises(RuntimeError, match="existe pero no es un directorio"),
        ):
            path_resolver.get_mo2_mods_path()

    def test_mo2_mods_path_apuntando_a_archivo_falla_cerrado(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """``MO2_MODS_PATH`` apuntando a un archivo (no directorio) es fail-closed."""
        archivo = tmp_path / "no_es_directorio.txt"
        archivo.write_text("x", encoding="utf-8")

        with (
            patch.dict(os.environ, {"MO2_MODS_PATH": str(archivo)}, clear=True),
            pytest.raises(RuntimeError, match="existe pero no es un directorio"),
        ):
            path_resolver.get_mo2_mods_path()


class TestResolveModlistSinSplitBrain:
    """``resolve_modlist_path`` deriva de la MISMA instancia que ``get_mo2_mods_path``.

    El split-brain pre-PR era: ``get_mo2_mods_path`` derivaba del INI mientras
    ``resolve_modlist_path`` seguía usando ``MO2_PATH/profiles`` (potencialmente
    otro árbol). Tras este PR ambos comparten la fuente
    :func:`descubrir_metadata_instancia_mo2`. Estos tests ejercitan tres
    configuraciones (portable, global, MO2_PATH de datos) y assertan que
    ``mods/`` y ``profiles/<profile>/`` cuelgan de la misma raíz de datos.
    """

    def test_modlist_y_mods_de_la_misma_instancia_global(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """Instancia global única: ``profiles`` y ``mods`` viven bajo la misma base."""
        exe_dir, dir_de_instancia, mods_dir, local_app_data = self._montar_instancia_global(tmp_path)
        # Crear el profile que vamos a consultar (orden: primero el directorio
        # padre, luego el archivo — pathlib.write_text no crea padres).
        (dir_de_instancia / "profiles" / "Default").mkdir(parents=True, exist_ok=True)
        (dir_de_instancia / "profiles" / "Default" / "modlist.txt").write_text("a", encoding="utf-8")

        with patch.dict(
            os.environ,
            {"MO2_PATH": str(exe_dir), "LOCALAPPDATA": str(local_app_data)},
            clear=True,
        ):
            mods = path_resolver.get_mo2_mods_path()
            modlist = path_resolver.resolve_modlist_path("Default")

        assert _mismo_path(mods, mods_dir)
        assert _mismo_path(modlist, dir_de_instancia / "profiles" / "Default" / "modlist.txt")
        # Mismo árbol: modlist.parent.parent.parent == mods.parent
        assert _mismo_path(modlist.parent.parent.parent, mods.parent)

    def test_modlist_desde_mo2_path_datos_legado(
        self,
        path_resolver: PathResolutionService,
        tmp_path: pathlib.Path,
    ) -> None:
        """Un ``MO2_PATH`` de datos (sin exe, con mods/ y profiles/) se respeta."""
        datos = tmp_path / "datos_legacy"
        mods_de_datos = datos / "mods"
        mods_de_datos.mkdir(parents=True)
        (datos / "profiles" / "Default" / "modlist.txt").parent.mkdir(parents=True)
        (datos / "profiles" / "Default" / "modlist.txt").write_text("a", encoding="utf-8")

        with patch.dict(
            os.environ,
            {"MO2_PATH": str(datos), "LOCALAPPDATA": str(tmp_path / "no_lappdata")},
            clear=True,
        ):
            mods = path_resolver.get_mo2_mods_path()
            modlist = path_resolver.resolve_modlist_path("Default")

        assert _mismo_path(mods, mods_de_datos)
        assert _mismo_path(modlist, datos / "profiles" / "Default" / "modlist.txt")

    @staticmethod
    def _montar_instancia_global(tmp_path: pathlib.Path):
        """Helper: replica el fixture de TestGetMo2ModsPathDeInstancia."""
        exe_dir = tmp_path / "Modding" / "ModOrganizer2"
        exe_dir.mkdir(parents=True)
        (exe_dir / "ModOrganizer.exe").write_bytes(b"fake exe")
        dir_de_instancia = tmp_path / "Modding" / "MO2" / "SkyrimSE"
        mods_dir = dir_de_instancia / "mods"
        mods_dir.mkdir(parents=True)
        local_app_data = tmp_path / "LocalAppData"
        ini_dir = local_app_data / "ModOrganizer" / "SkyrimSE"
        ini_dir.mkdir(parents=True)
        (ini_dir / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(base_directory=_formato_qt(dir_de_instancia)),
            encoding="utf-8",
        )
        return exe_dir, dir_de_instancia, mods_dir, local_app_data


class TestWiringProductivoAppContext:
    """El wiring de producción AppContext → PathValidator → PathResolutionService
    debe cerrar el gap de la instancia separada.

    Sin la raíz de la instancia en el sandbox, ``get_mo2_mods_path`` fallaría
    con "fuera de las raíces permitidas del sandbox" para una instalación
    global donde ``ModOrganizer.exe`` y ``base_directory`` viven en raíces
    distintas — el bug exacto del rig. Esta clase verifica que
    :func:`_construir_raices_sandbox` registra la raíz de la instancia
    detectada vía metadata, y que el ``PathResolutionService`` construido
    con ese validator resuelve la ruta correcta.
    """

    def test_sandbox_de_app_context_registra_raiz_de_instancia_y_resuelve(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        from sky_claw.app.core.path_resolver import PathResolutionService
        from sky_claw.app.security.path_validator import PathValidator
        from sky_claw.app_context import _construir_raices_sandbox

        # Replica el rig: exe en un árbol, instance base en otro.
        exe_dir = tmp_path / "Apps" / "MO2"
        exe_dir.mkdir(parents=True)
        (exe_dir / "ModOrganizer.exe").write_bytes(b"fake")
        instance_dir = tmp_path / "Modding" / "MO2" / "SkyrimSE"
        mods_dir = instance_dir / "mods"
        mods_dir.mkdir(parents=True)
        local_app_data = tmp_path / "LocalAppData"
        ini_dir = local_app_data / "ModOrganizer" / "SkyrimSE"
        ini_dir.mkdir(parents=True)
        (ini_dir / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(base_directory=_formato_qt(instance_dir)),
            encoding="utf-8",
        )

        # El seam lee LOCALAPPDATA; el conftest ya lo parchó a un dir vacío
        # por test, así que lo sobreescribimos con el nuestro para que la
        # instancia esté visible.
        with patch.dict(
            os.environ,
            {"MO2_PATH": str(exe_dir), "LOCALAPPDATA": str(local_app_data)},
            clear=True,
        ):
            # Las raíces se componen con la misma función que usa AppContext.
            roots = _construir_raices_sandbox(
                mo2_root=exe_dir,
                install_dir=None,
                skyrim_path=None,
            )
            # La raíz de la instancia DEBE estar registrada por el seam.
            assert instance_dir in roots, f"raíz de instancia no registrada: {roots}"
            assert exe_dir in roots

            # El resolver construido con ese validator cierra el gap.
            resolver = PathResolutionService(
                path_validator=PathValidator(roots=roots),
                profile_name="Default",
            )
            resultado = resolver.get_mo2_mods_path()
            modlist = resolver.resolve_modlist_path("Default")
        assert _mismo_path(resultado, mods_dir)
        # profiles/<Default>/modlist.txt ni siquiera existe, pero el path
        # construido se valida y vive bajo la misma raíz de instancia.
        assert _mismo_path(modlist.parent.parent.parent, mods_dir.parent)

    def test_sin_seam_el_mismo_wiring_falla_cerrado(
        self,
        tmp_path: pathlib.Path,
        tmp_path_factory,
    ) -> None:
        """Composición paralela: mismas roots SIN la raíz de instancia → RuntimeError.

        Demuestra que sin :func:`_construir_raices_sandbox` el wiring de
        producción NO puede operar (la pre-PR configuración). El seam es lo
        que cierra la grieta.
        """
        from sky_claw.app.core.path_resolver import PathResolutionService
        from sky_claw.app.security.path_validator import PathValidator

        exe_dir = tmp_path / "exe"
        exe_dir.mkdir(parents=True)
        (exe_dir / "ModOrganizer.exe").write_bytes(b"fake")
        # La instancia vive FUERA del sandbox declarado: un directorio hermano
        # generado con ``tmp_path_factory`` (no bajo ``tmp_path``).
        instance_dir = tmp_path_factory.mktemp("instance_fuera_sandbox")
        mods_dir = instance_dir / "mods"
        mods_dir.mkdir(parents=True)
        local_app_data = tmp_path / "LocalAppData"
        ini_dir = local_app_data / "ModOrganizer" / "Inst"
        ini_dir.mkdir(parents=True)
        (ini_dir / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(base_directory=_formato_qt(instance_dir)),
            encoding="utf-8",
        )

        # Sin el seam: sandbox = {exe_dir, tmp_path}. La instancia queda fuera.
        roots = [exe_dir, tmp_path]
        assert instance_dir not in roots  # sanity: instance realmente fuera
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=roots),
            profile_name="Default",
        )
        with (
            patch.dict(
                os.environ,
                {"MO2_PATH": str(exe_dir), "LOCALAPPDATA": str(local_app_data)},
                clear=True,
            ),
            pytest.raises(RuntimeError, match="fuera de las raíces permitidas"),
        ):
            resolver.get_mo2_mods_path()

    def test_seam_rechaza_base_directory_en_raiz_de_unidad(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """Si ``base_directory`` apunta a una raíz de unidad (``G:\\``), el seam la
        rechaza para no ampliar el sandbox a una unidad completa.
        """
        from sky_claw.app_context import _raiz_datos_instancia_para_sandbox

        exe_dir = tmp_path / "exe"
        exe_dir.mkdir(parents=True)
        (exe_dir / "ModOrganizer.exe").write_bytes(b"fake")
        ini_dir = tmp_path / "LocalAppData" / "ModOrganizer" / "Inst"
        ini_dir.mkdir(parents=True)
        ini_dir = ini_dir
        # base_directory = unidad raíz (e.g. "G:/" en este rig).
        (ini_dir / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(base_directory=_formato_qt(tmp_path.anchor)),
            encoding="utf-8",
        )

        with patch.dict(
            os.environ,
            {"MO2_PATH": str(exe_dir), "LOCALAPPDATA": str(tmp_path / "LocalAppData")},
            clear=True,
        ):
            raiz = _raiz_datos_instancia_para_sandbox(exe_dir)
        assert raiz is None, f"drive root no debió registrarse como raíz de sandbox: {raiz}"


class TestAnclaConstructoresManualesDeMods:
    """Congela los sitios de producción que construyen ``<raíz>/mods`` a mano.

    El defecto que este PR cierra es derivar ``mods/`` de la instalación del
    ejecutable; ``get_mo2_mods_path()`` es la pieza centralizada del concepto
    "directorio de mods de la instancia activa". Cada sitio que construye la
    ruta por su cuenta queda enumerado con su racional — un constructor nuevo
    rompe el ancla hasta que alguien decida si centraliza o lo exime con un
    racional (regla del repo: la exclusión se justifica, no se muestra).
    """

    # módulo relativo -> líneas con `expr / "mods"` (RHS literal)
    _CONSTRUCTORES: dict[str, tuple[int, ...]] = {
        # El propio resolver construye `<base>/mods` desde la metadata de la
        # instancia (mismo concepto que centraliza este PR: la "raíz de datos"
        # de la instancia activa). Aparece en el helper de deferencia
        # (228), en la construcción de la metadata del modo "mo2_path_datos"
        # (288) y en el paso legacy de get_mo2_mods_path (903). Cualquier
        # nueva construcción de `<base>/mods` debe ir por estas tres rutas
        # o extender el ancla con su racional.
        "sky_claw/app/core/path_resolver.py": (228, 288, 903),
        # Instaladores NGIO/FOMOD del agente LLM sobre mo2.root del registry:
        # superficie agente, layout portable asumido — fuera de alcance (PR-0).
        "sky_claw/app/agent/tools/external_tools.py": (239, 288),
        "sky_claw/app/agent/tools/system_tools.py": (279,),
        "sky_claw/local/fomod/plugin_state.py": (106, 158, 162),
        # Broker VFS real: su raíz EXIGE ModOrganizer.exe + árbol de datos
        # juntos (layout portable por contrato) — no es la instancia de la GUI.
        "sky_claw/local/mo2/vfs.py": (310,),
        "sky_claw/local/mo2/vfs_attestation.py": (189, 191, 233),
        # Detectores de estado de mods instalados (Community Shaders) sobre la
        # raíz que detectó el scanner: concepto de detección, no de instancia.
        "sky_claw/local/discovery/scanner.py": (458,),
        "sky_claw/app/gui/controllers/ritual_runner.py": (1033,),
        # Preflight/preview/checkers read-only sobre mo2 raw/validado. Cada
        # uno es el DEFAULT histórico ``<raíz>/mods`` que solo se usa cuando el
        # caller no pasó un MODS_DIR declarado (``mods_dir=``): con
        # ``mod_directory`` custom el valor declarado manda (get_mo2_mods_path*).
        "sky_claw/local/tools/loot_service.py": (488,),
        "sky_claw/local/validators/vfs_health.py": (134,),
        "sky_claw/local/validators/preflight_sensors.py": (177,),
        "sky_claw/app/orchestrator/preview/chain_preview_service.py": (323,),
        # Rollback/move-aside y staging de DynDOLOD bajo el árbol del broker.
        "sky_claw/app_context.py": (1331,),
        "sky_claw/local/tools/rollback_reconciler.py": (236,),
        "sky_claw/local/tools/output_targets.py": (157,),
        "sky_claw/local/mo2/grass_profile.py": (227, 330),
    }

    @staticmethod
    def _lineas_div_mods(texto: str) -> tuple[int, ...]:
        """Líneas con ``<expr> / "mods"`` (RHS literal) en un módulo."""
        arbol = ast.parse(texto)
        return tuple(
            sorted(
                nodo.lineno
                for nodo in ast.walk(arbol)
                if isinstance(nodo, ast.BinOp)
                and isinstance(nodo.op, ast.Div)
                and isinstance(nodo.right, ast.Constant)
                and nodo.right.value == "mods"
            )
        )

    def test_constructores_enumerados_y_congelados(self) -> None:
        """Cada módulo con `expr / "mods"` está en el mapa con su racional."""
        raiz = pathlib.Path(__file__).resolve().parents[1] / "sky_claw"
        hallados: dict[str, tuple[int, ...]] = {}
        for py in sorted(raiz.rglob("*.py")):
            texto = py.read_text(encoding="utf-8")
            try:
                lineas = self._lineas_div_mods(texto)
            except SyntaxError:
                continue
            if lineas:
                hallados[str(py.relative_to(raiz.parents[0])).replace("\\", "/")] = lineas

        assert hallados == self._CONSTRUCTORES, (
            "El inventario de constructores manuales de mods cambió. Si el sitio "
            "nuevo representa el mismo concepto que get_mo2_mods_path(), "
            "centralízalo; si no, actualiza el mapa con su racional."
        )


class TestInstalacionMo2Inyectada:
    """La instalación MO2 inyectada por el composition root (``AppContext``)
    manda sobre ``MO2_PATH``/auto-detección en el resolver.

    Bloqueante del PR #552: ``AppContext`` seleccionaba una instalación y armaba
    el sandbox con ella, pero ``PathResolutionService`` volvía a decidir por su
    cuenta vía ``MO2_PATH`` → ``detect_mo2_path()``. Como la instalación es la
    que localiza el ``ModOrganizer.ini`` portable, decidir distinto significa
    leer la metadata de OTRA instancia (split-brain
    instalación-seleccionada != instalación-usada): sandbox de A, metadata de B.
    Con el código anterior (``mo2_install_dir`` inexistente) el caso de
    divergencia devolvía ``mods`` de B; con la inyección devuelve el de A.
    """

    @staticmethod
    def _montar_dos_instalaciones(
        tmp_path: pathlib.Path,
    ) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
        """Dos instalaciones portables MO2, cada una con su instancia de datos.

        ``selected`` es la que elige ``AppContext``; ``wrong_auto`` es la que
        elegiría ``MO2_PATH``/la auto-detección. Cada exe tiene su
        ``ModOrganizer.ini`` portable apuntando a una base distinta, así que la
        instalación usada determina de forma unívoca el ``mods/`` resuelto.

        Returns:
            (selected, wrong_auto, mods_selected, mods_wrong)
        """
        selected = tmp_path / "selected"
        wrong_auto = tmp_path / "wrong_auto"
        data_selected = tmp_path / "instance_selected"
        data_wrong = tmp_path / "instance_wrong"
        mods_selected = data_selected / "mods"
        mods_wrong = data_wrong / "mods"
        for d in (selected, wrong_auto, mods_selected, mods_wrong):
            d.mkdir(parents=True)
        (selected / "ModOrganizer.exe").write_bytes(b"fake exe")
        (wrong_auto / "ModOrganizer.exe").write_bytes(b"fake exe")
        (selected / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(base_directory=_formato_qt(data_selected)),
            encoding="utf-8",
        )
        (wrong_auto / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(base_directory=_formato_qt(data_wrong)),
            encoding="utf-8",
        )
        return selected, wrong_auto, mods_selected, mods_wrong

    def test_divergencia_hint_gana_sobre_mo2_path(self, tmp_path: pathlib.Path) -> None:
        """Bloqueante / Caso C: hint=selected, MO2_PATH=wrong_auto → selected.

        Modela deliberadamente DOS instalaciones. Con el código anterior el
        resolver leía la metadata de ``wrong_auto`` (MO2_PATH) y devolvía
        ``mods_wrong``; con la inyección devuelve ``mods_selected`` y el modlist
        cuelga de la MISMA instancia (sin split-brain). El sandbox incluye ambas
        instalaciones e instancias, así que la divergencia se distingue por el
        RESULTADO, no por un fallo de validación.
        """
        selected, wrong_auto, mods_selected, mods_wrong = self._montar_dos_instalaciones(tmp_path)
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=selected,
        )
        with patch.dict(os.environ, {"MO2_PATH": str(wrong_auto)}, clear=True):
            mods = resolver.get_mo2_mods_path()
            modlist = resolver.resolve_modlist_path("Default")
        assert _mismo_path(mods, mods_selected)
        assert not _mismo_path(mods, mods_wrong)
        # modlist deriva de la misma raíz de instancia que mods (anti split-brain).
        assert _mismo_path(modlist.parent.parent.parent, mods_selected.parent)

    def test_sin_hint_usa_mo2_path_regresion_a(self, tmp_path: pathlib.Path) -> None:
        """Caso A (regresión standalone): sin inyección, MO2_PATH configurado.

        ``mo2_install_dir=None`` conserva el comportamiento legacy: la
        instalación la decide ``MO2_PATH``.
        """
        selected, wrong_auto, _mods_selected, mods_wrong = self._montar_dos_instalaciones(tmp_path)
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=None,
        )
        with patch.dict(os.environ, {"MO2_PATH": str(wrong_auto)}, clear=True):
            mods = resolver.get_mo2_mods_path()
        assert _mismo_path(mods, mods_wrong)

    def test_sin_hint_ni_env_cae_a_autodeteccion_regresion_b(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """Caso B (regresión): sin hint ni MO2_PATH → ``detect_mo2_path()``."""
        selected, _wrong_auto, mods_selected, _mods_wrong = self._montar_dos_instalaciones(tmp_path)
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=None,
        )
        with (
            patch(
                "sky_claw.app.core.path_resolver._CANDIDATE_MO2_PATHS",
                (str(selected),),
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            mods = resolver.get_mo2_mods_path()
        assert _mismo_path(mods, mods_selected)

    def test_hint_sin_ini_portable_resuelve_instancia_global_caso_d(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """Caso D: el hint sin INI portable no bloquea la instancia global.

        La instalación inyectada es solo una PISTA para localizar el
        ``ModOrganizer.ini`` portable. Si no hay INI junto al exe, la única
        instancia global bajo ``%LOCALAPPDATA%\\ModOrganizer`` sigue
        resolviéndose (no se confunde ``mo2_install_dir`` con la raíz de datos).
        """
        selected = tmp_path / "selected"
        selected.mkdir()
        (selected / "ModOrganizer.exe").write_bytes(b"fake exe")  # sin ModOrganizer.ini
        data_global = tmp_path / "instance_global"
        mods_global = data_global / "mods"
        mods_global.mkdir(parents=True)
        local_app_data = tmp_path / "LocalAppData"
        ini_dir = local_app_data / "ModOrganizer" / "SkyrimSE"
        ini_dir.mkdir(parents=True)
        (ini_dir / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(base_directory=_formato_qt(data_global)),
            encoding="utf-8",
        )
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=selected,
        )
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}, clear=True):
            # La rama 1 (hint) SÍ se ejecuta y devuelve `selected` (no se ignora
            # el hint): lo que cae a global es la resolución de METADATA, porque
            # `selected` no tiene INI portable — no la selección de instalación.
            assert _mismo_path(resolver._directorio_instalacion_mo2(), selected)
            mods = resolver.get_mo2_mods_path()
        assert _mismo_path(mods, mods_global)

    def test_hint_valido_con_metadata_fuera_del_sandbox_falla_cerrado(
        self,
        tmp_path: pathlib.Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Un hint válido cuyo INI portable apunta FUERA del sandbox falla cerrado.

        Asimetría que podría abrir el cable: el hint valida contra el sandbox
        (está bajo ``tmp_path``), pero el ``base_directory`` del ``ModOrganizer.ini``
        portable apunta a un árbol NO sandboxeado. ``_metadata_de_instancia``
        valida ``raiz_datos`` **y** ``mods`` de la metadata contra el sandbox, así
        que la resolución falla cerrado en vez de operar sobre un path externo —
        el mismo contrato que ``test_t6`` prueba para ``MO2_PATH``, ahora por la
        puerta del hint inyectado.
        """
        selected = tmp_path / "selected"
        selected.mkdir()
        (selected / "ModOrganizer.exe").write_bytes(b"fake exe")
        # base_directory FUERA del sandbox: un árbol generado con
        # ``tmp_path_factory`` (no bajo ``tmp_path``), como en
        # ``test_sin_seam_el_mismo_wiring_falla_cerrado``.
        externo = tmp_path_factory.mktemp("instance_externa_mo2")
        (externo / "mods").mkdir(parents=True)
        (selected / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(base_directory=_formato_qt(externo)),
            encoding="utf-8",
        )
        resolver = PathResolutionService(
            # Sandbox = solo tmp_path; `externo` (la metadata) queda fuera.
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=selected,
        )
        # sanity: la metadata realmente vive fuera del sandbox (no bajo tmp_path).
        assert not _mismo_path(externo, tmp_path) and tmp_path not in externo.parents
        with (
            patch.dict(os.environ, {}, clear=True),
            pytest.raises(RuntimeError, match="fuera de las raíces permitidas"),
        ):
            resolver.get_mo2_mods_path()

    def test_hint_fuera_del_sandbox_no_degrada_al_entorno(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """Un hint inválido NO cae a ``MO2_PATH`` **ni** a ``detect_mo2_path``.

        Degradar al entorno reintroduciría el split-brain que la inyección
        cierra. Cuando hay hint, ``_directorio_instalacion_mo2`` NUNCA alcanza
        las ramas 2 (``MO2_PATH``) ni 3 (auto-detección): si el hint no valida
        devuelve ``None`` y la resolución sigue por metadata global/fail-closed.

        El escenario es adversarial a propósito (cierra el hallazgo del review
        pr-agent "Regresión funcional"): ambas puertas del entorno apuntan a una
        instalación **válida dentro del sandbox** (``wrong_auto`` vía
        ``MO2_PATH`` y vía un candidato de ``detect_mo2_path``). Si el resolver
        degradara por cualquiera de las dos, devolvería ``wrong_auto/mods`` en
        vez de fallar; que lance ``RuntimeError`` prueba que ninguna se consulta
        con un hint presente. Sin instancia global (``LOCALAPPDATA`` ausente por
        ``clear=True``) la resolución no tiene de dónde sacar metadata → falla
        cerrado.
        """
        selected, wrong_auto, _mods_selected, _mods_wrong = self._montar_dos_instalaciones(tmp_path)
        # Sandbox = solo wrong_auto + su instancia; `selected` (el hint) queda fuera.
        sandbox_roots = [wrong_auto, tmp_path / "instance_wrong"]
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=sandbox_roots),
            profile_name="Default",
            mo2_install_dir=selected,
        )
        with (
            # detect_mo2_path devolvería wrong_auto (válido en el sandbox) SI se
            # llamara — no debe llamarse con un hint presente.
            patch(
                "sky_claw.app.core.path_resolver._CANDIDATE_MO2_PATHS",
                (str(wrong_auto),),
            ),
            # MO2_PATH también válido en el sandbox — tampoco debe consultarse.
            patch.dict(os.environ, {"MO2_PATH": str(wrong_auto)}, clear=True),
            pytest.raises(RuntimeError),
        ):
            resolver.get_mo2_mods_path()

    def test_directorio_instalacion_precedencia_hint_env_detect(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """``_directorio_instalacion_mo2``: hint > MO2_PATH > detect_mo2_path."""
        selected, wrong_auto, _ms, _mw = self._montar_dos_instalaciones(tmp_path)
        validator = PathValidator(roots=[tmp_path])

        # 1. Hint inyectado gana aunque MO2_PATH apunte a otra instalación.
        con_hint = PathResolutionService(path_validator=validator, mo2_install_dir=selected)
        with patch.dict(os.environ, {"MO2_PATH": str(wrong_auto)}, clear=True):
            assert _mismo_path(con_hint._directorio_instalacion_mo2(), selected)

        # 2/3. Sin hint: MO2_PATH gana sobre detect; sin ninguno, detect.
        sin_hint = PathResolutionService(path_validator=validator)
        with patch(
            "sky_claw.app.core.path_resolver._CANDIDATE_MO2_PATHS",
            (str(selected),),
        ):
            with patch.dict(os.environ, {"MO2_PATH": str(wrong_auto)}, clear=True):
                assert _mismo_path(sin_hint._directorio_instalacion_mo2(), wrong_auto)
            with patch.dict(os.environ, {}, clear=True):
                assert _mismo_path(sin_hint._directorio_instalacion_mo2(), selected)


class TestGetMo2PathHonraInstalacionSeleccionada:
    """``get_mo2_path()`` (accessor de tools) honra la instalación inyectada.

    Split-brain restante después de #554: ``_directorio_instalacion_mo2`` (la
    fuente de mods/metadata) ya priorizaba el hint del composition root, pero
    ``get_mo2_path()`` —el accessor que consumen DynDOLOD, Synthesis, Wrye
    Bash, Pandora, LOOT, xEdit, grass, preview y el sandbox de Synthesis—
    seguía leyendo SOLO ``MO2_PATH``. Con AppContext=A y MO2_PATH=B, la
    instalación que decidía mods/ era A y la que veían los accessors de tools
    era B: preflights y outputs apuntando a otra instalación.

    Contrato nuevo (misma precedencia que ``_directorio_instalacion_mo2``
    SIN auto-detección): inyectado validado → MO2_PATH validado → None.
    El hint que no valida NO degrada al entorno (reintroduciría el
    split-brain): falla cerrado a ``None``.
    """

    @staticmethod
    def _montar_instalaciones(
        tmp_path: pathlib.Path,
    ) -> tuple[pathlib.Path, pathlib.Path]:
        """Dos instalaciones MO2 dentro del sandbox: ``selected`` y ``env``.

        Sólo importan como directorios: ``get_mo2_path()`` valida y devuelve,
        no lee metadata. Se crean en disco porque el accessor exige un
        directorio real (capability gate): ``PathValidator.validate()`` usa
        ``resolve()`` no estricto y no exige existencia por sí solo.
        """
        selected = tmp_path / "selected"
        env_install = tmp_path / "wrong-env"
        selected.mkdir()
        env_install.mkdir()
        return selected, env_install

    def test_inyectada_gana_sobre_mo2_path(self, tmp_path: pathlib.Path) -> None:
        """Caso P1 / I1: hint=selected, MO2_PATH=wrong-env → selected.

        Reproduce el defecto ANTES del fix: ``get_mo2_path()`` ignoraba el
        hint y devolvía ``wrong-env`` (la instalación del entorno), mientras
        la metadata de mods/ colgaba de ``selected`` — split-brain entre el
        accessor de tools y la resolución de instancia.
        """
        selected, env_install = self._montar_instalaciones(tmp_path)
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=selected,
        )
        with patch.dict(os.environ, {"MO2_PATH": str(env_install)}, clear=True):
            ruta = resolver.get_mo2_path()
        assert ruta is not None
        assert _mismo_path(ruta, selected)
        assert not _mismo_path(ruta, env_install)

    def test_sin_inyeccion_conserva_mo2_path(self, tmp_path: pathlib.Path) -> None:
        """I2: sin hint, MO2_PATH configurado → MO2_PATH (legacy intacto)."""
        selected, env_install = self._montar_instalaciones(tmp_path)
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=None,
        )
        with patch.dict(os.environ, {"MO2_PATH": str(env_install)}, clear=True):
            ruta = resolver.get_mo2_path()
        assert ruta is not None
        assert _mismo_path(ruta, env_install)

    def test_sin_inyeccion_ni_env_devuelve_none(self, tmp_path: pathlib.Path) -> None:
        """I3: sin hint ni MO2_PATH → ``None`` (SIN auto-detección nueva).

        ``get_mo2_path()`` nunca auto-detectó; convertir ``None`` en
        "intenta detectar" cambiaría el significado de capability gates como
        ``get_mo2_path() is not None`` (LOOT/xEdit/previews) y dispararía
        escaneos del filesystem real en cada llamada.
        """
        selected, _env_install = self._montar_instalaciones(tmp_path)
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=None,
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            # Adversarial: una instalación detectable existe; que siga en
            # ``None`` prueba que el accessor NO llama a detect_mo2_path().
            patch(
                "sky_claw.app.core.path_resolver._CANDIDATE_MO2_PATHS",
                (str(selected),),
            ),
        ):
            ruta = resolver.get_mo2_path()
        assert ruta is None

    def test_inyectada_fuera_del_sandbox_falla_cerrado_a_none(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """I4: hint fuera del sandbox + MO2_PATH válido → ``None``, NO degrada.

        Degradar a B reharía el split-brain: instalación seleccionada A
        (invalidada), instalación usada B. ``None`` es el "sin instalación
        conocida" del fail-closed, misma decisión que toma
        ``_directorio_instalacion_mo2`` con el hint inválido.
        """
        selected, env_install = self._montar_instalaciones(tmp_path)
        # Sandbox = solo el entorno; `selected` (el hint) queda fuera.
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[env_install]),
            profile_name="Default",
            mo2_install_dir=selected,
        )
        with patch.dict(os.environ, {"MO2_PATH": str(env_install)}, clear=True):
            ruta = resolver.get_mo2_path()
        assert ruta is None

    def test_path_con_espacios_se_honra(self, tmp_path: pathlib.Path) -> None:
        """E: instalación inyectada con espacios → se devuelve igual.

        Las instalaciones reales viven en rutas como ``C:\\Modding\\MO2 Portátil``:
        el accessor no debe corromperlas ni rechazarlas por espacios.
        """
        selected = tmp_path / "MO2 Selected Dir"
        selected.mkdir()
        env_install = tmp_path / "MO2 Env"
        env_install.mkdir()
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=selected,
        )
        with patch.dict(os.environ, {"MO2_PATH": str(env_install)}, clear=True):
            ruta = resolver.get_mo2_path()
        assert ruta is not None
        assert _mismo_path(ruta, selected)

    @symlink_guard
    def test_symlink_del_hint_que_escapa_sandbox_falla_cerrado(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """F: symlink del hint que apunta FUERA del sandbox → ``None``.

        El hint inyectado NO es confiable por venir del composition root
        (FASE 9): pasa por el mismo ``validate_env_path`` que ``MO2_PATH``,
        incluido el chequeo estricto de symlinks de ``PathValidator``. Un
        symlink que escapa degrada a ``None`` sin degradar a ``MO2_PATH``.
        """
        externo = pathlib.Path(tempfile.mkdtemp(prefix="mo2_hint_externo_"))
        try:
            externo.mkdir(exist_ok=True)
            link = tmp_path / "selected_link"
            link.symlink_to(externo, target_is_directory=True)
            env_install = tmp_path / "wrong-env"
            env_install.mkdir(exist_ok=True)
            resolver = PathResolutionService(
                path_validator=PathValidator(roots=[tmp_path]),
                profile_name="Default",
                mo2_install_dir=link,
            )
            with patch.dict(os.environ, {"MO2_PATH": str(env_install)}, clear=True):
                ruta = resolver.get_mo2_path()
            assert ruta is None
        finally:
            shutil.rmtree(externo, ignore_errors=True)

    @symlink_guard
    def test_symlink_del_hint_dentro_del_sandbox_se_resuelve(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """F (contraparte): symlink del hint que apunta DENTRO del sandbox → válido.

        ``PathValidator.validate`` resuelve el symlink y devuelve el destino:
        el accessor devuelve el destino canonicalizado, igual que hace
        ``MO2_PATH`` desde siempre (semántica del validate, no una nueva).
        """
        real = tmp_path / "selected_real"
        real.mkdir()
        link = tmp_path / "selected_link"
        link.symlink_to(real, target_is_directory=True)
        env_install = tmp_path / "wrong-env"
        env_install.mkdir(exist_ok=True)
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=link,
        )
        with patch.dict(os.environ, {"MO2_PATH": str(env_install)}, clear=True):
            ruta = resolver.get_mo2_path()
        assert ruta is not None
        # El validate devuelve el destino resuelto (comportamiento MO2_PATH legacy).
        assert _mismo_path(ruta, real)

    def test_inyectada_coherente_con_metadata_y_modlist(self, tmp_path: pathlib.Path) -> None:
        """I5: con hint, accessor e instancia derivan de la MISMA instalación.

        El ``ModOrganizer.ini`` portable del hint declara la base de datos:
        ``get_mo2_path()`` (fix) y ``get_mo2_mods_path()``/``resolve_modlist_path()``
        (ya desde #554) cuelgan todos de ``selected`` — el modlist de la
        instancia de ``wrong-env`` jamás se consulta.
        """
        selected, env_install = self._montar_instalaciones(tmp_path)
        data_selected = tmp_path / "instance_selected"
        mods_selected = data_selected / "mods"
        profile_dir = data_selected / "profiles" / "Default"
        mods_selected.mkdir(parents=True)
        profile_dir.mkdir(parents=True)
        (selected / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(base_directory=_formato_qt(data_selected)),
            encoding="utf-8",
        )
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=selected,
        )
        with patch.dict(os.environ, {"MO2_PATH": str(env_install)}, clear=True):
            ruta = resolver.get_mo2_path()
            mods = resolver.get_mo2_mods_path()
            modlist = resolver.resolve_modlist_path("Default")
        assert ruta is not None
        assert _mismo_path(ruta, selected)
        assert _mismo_path(mods, mods_selected)
        assert _mismo_path(modlist.parent.parent.parent, data_selected)

    def test_crudo_con_inyectada_devuelve_la_seleccionada_sin_resolver(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """Hermano crudo: con composición, ``get_mo2_path_raw`` = instalada.

        El preflight de LOOT cruza el accessor VALIDADO (capability gate) con
        el CRUDO (lstat de symlinks). Si el crudo siguiera leyendo solo
        ``MO2_PATH``, el sensor VFS inspeccionaría el árbol del entorno
        mientras las tools operan sobre la inyectada — otra forma del
        split-brain.
        """
        selected, env_install = self._montar_instalaciones(tmp_path)
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=selected,
        )
        with patch.dict(os.environ, {"MO2_PATH": str(env_install)}, clear=True):
            crudo = resolver.get_mo2_path_raw()
            ruta = resolver.get_mo2_path()
        assert crudo is not None
        assert _mismo_path(crudo, selected)
        assert ruta is not None
        assert _mismo_path(ruta, selected)

    def test_crudo_sin_inyeccion_conserva_mo2_path(self, tmp_path: pathlib.Path) -> None:
        """Hermano crudo sin composición: legacy intacto (MO2_PATH)."""
        selected, env_install = self._montar_instalaciones(tmp_path)
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=None,
        )
        with patch.dict(os.environ, {"MO2_PATH": str(env_install)}, clear=True):
            crudo = resolver.get_mo2_path_raw()
        assert crudo is not None
        assert _mismo_path(crudo, env_install)

    def test_crudo_sin_inyeccion_ni_env_devuelve_none(self, tmp_path: pathlib.Path) -> None:
        """Hermano crudo sin nada: ``None``, sin auto-detección (legacy)."""
        selected, _env_install = self._montar_instalaciones(tmp_path)
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=None,
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "sky_claw.app.core.path_resolver._CANDIDATE_MO2_PATHS",
                (str(selected),),
            ),
        ):
            crudo = resolver.get_mo2_path_raw()
        assert crudo is None


class TestAnclaSemanticaDeRaicesMo2:
    """Ancla semántica de los call sites productivos de raíces MO2.

    INSTALL_ROOT (``get_mo2_path``) e INSTANCE_DATA_ROOT
    (``get_mo2_instance_data_root``) no son intercambiables desde MO2 2.4: la
    instalación (``ModOrganizer.exe``) y los datos (``profiles/``,
    ``overwrite/``, ``mods/``) pueden vivir en discos distintos. Un consumer
    nuevo SIN clasificar aquí revive el split-brain en silencio: el test
    fuerza a responder "¿qué tipo de raíz necesita?" antes de pasar CI.

    Clasificación congelada (HEAD con el fix, por módulo):

    INSTALL_ROOT / CAPABILITY (``get_mo2_path``):

    - grass_runtime_deps: DEUDA BLOQUEANTE #557 (no olvido deliberado sin
      cerrar): ``MO2Controller``/``GrassProfileManager`` son monorraíz — el
      MISMO root alimenta ``launch_game`` (necesita INSTALL:
      ``ModOrganizer.exe``) y ``profiles/``+``mods/``+``overwrite/`` (necesitan
      DATA). Migrar solo el provider a datos rompería el lanzamiento; la
      corrección exige separar las raíces dentro de ``MO2Controller``
      (issue #557, bloquea declarar cerrada la familia split-brain de #552 en
      rigs con instancia separada).
    - dyndolod_service (runner): ``DynDOLODConfig.mo2_path`` no se usa en
      ejecución (solo ``mo2_mods_path``/outputs); INSTALL opaco.
    - wrye_bash_service (runner): ``WryeBashConfig.mo2_path`` sin uso en
      ejecución headless; INSTALL opaco.
    - xedit_service: capability gate del scan (no deriva paths de datos; el
      raw preserva la detección real de links sobre install).

    INSTANCE_DATA_ROOT (``get_mo2_instance_data_root``):

    - dispatcher_dependencies: ``ProfileSandbox`` clona profiles/overwrite.
    - chain_preview_service: ``mods/``/``overwrite/`` del preview (+ señal
      explícita antes de ``detect_mo2_path``).
    - dyndolod_service (preflight): overwrite + profile sources.
    - loot_service (3): preflight (sources/overwrite, + señal antes de
      detect), ``LoadOrderFileResolver`` (profiles/<perfil>/) y runner
      brokered (attestation autoconsistente sobre el perfil).
    - pandora_service: overwrite + perfil.
    - synthesis_service (2): output target y preflight.
    - wrye_bash_service (preflight): overwrite + profile sources.

    RAW_INSTALL_FOR_LSTAT (``get_mo2_path_raw``): solo xedit (VFS lstat sobre
    install). Los preflights migrados usan la raíz de datos validada como raw
    (no existe representación cruda sin resolver de la metadata).
    """

    #: Módulo → n.º de llamadas a ``get_mo2_path()`` (INSTALL/CAPABILITY).
    _INSTALL_O_CAPABILITY: dict[str, int] = {
        "sky_claw/app/orchestrator/grass_runtime_deps.py": 1,
        "sky_claw/local/tools/dyndolod_service.py": 1,
        "sky_claw/local/tools/wrye_bash_service.py": 1,
        "sky_claw/local/tools/xedit_service.py": 1,
    }

    #: Módulo → n.º de llamadas a ``get_mo2_instance_data_root()``.
    _INSTANCE_DATA: dict[str, int] = {
        "sky_claw/app/orchestrator/dispatcher_dependencies.py": 1,
        "sky_claw/app/orchestrator/preview/chain_preview_service.py": 1,
        "sky_claw/local/tools/dyndolod_service.py": 1,
        "sky_claw/local/tools/loot_service.py": 3,
        "sky_claw/local/tools/pandora_service.py": 1,
        "sky_claw/local/tools/synthesis_service.py": 2,
        "sky_claw/local/tools/wrye_bash_service.py": 1,
    }

    #: Módulo → n.º de usos de ``get_mo2_mods_path()`` (MODS_DIR estricto,
    #: falla cerrado con evidencia). asset_conflict_scan (escaneo de
    #: conflictos) y dyndolod (runner/permisos/preview): los consumidores de
    #: siempre; un sitio nuevo debe declarar su severidad.
    _MODS_ESTRICTO: dict[str, int] = {
        "sky_claw/app/orchestrator/asset_conflict_scan.py": 1,
        "sky_claw/local/tools/dyndolod_service.py": 3,
    }

    #: Módulo → n.º de usos de ``get_mo2_mods_path_best_effort()``: sensores
    #: read-only donde "sin mods" = omitir el sensor (lección #250). NO es
    #: severidad válida para un destino de escritura (ver ``para_destino``).
    _MODS_BEST_EFFORT: dict[str, int] = {
        "sky_claw/app/orchestrator/preview/chain_preview_service.py": 1,
        "sky_claw/local/tools/dyndolod_service.py": 1,
        "sky_claw/local/tools/loot_service.py": 1,
        "sky_claw/local/tools/pandora_service.py": 1,
        "sky_claw/local/tools/synthesis_service.py": 1,
        "sky_claw/local/tools/wrye_bash_service.py": 1,
    }

    #: Módulo → n.º de usos de ``get_mo2_mods_path_para_destino()`` (destino
    #: de ESCRITURA: aborta con evidencia si la instancia declaró mods y no
    #: resuelve — B de #555; contado como referencia enlazada/callable). Hoy
    #: solo Synthesis (runner + preflight, misma fuente U-01 parte 2).
    _MODS_PARA_DESTINO: dict[str, int] = {
        "sky_claw/local/tools/synthesis_service.py": 2,
    }

    #: Módulo → n.º de llamadas a ``get_mo2_path_raw()`` (raw install).
    _RAW_INSTALL: dict[str, int] = {
        "sky_claw/local/tools/xedit_service.py": 1,
    }

    #: Módulo → n.º de guards ``has_explicit_mo2_install_selection()``.
    _SENAL_EXPLICITA: dict[str, int] = {
        "sky_claw/app/orchestrator/preview/chain_preview_service.py": 1,
        "sky_claw/local/tools/loot_service.py": 1,
    }

    @staticmethod
    def _inventario(raiz: pathlib.Path, atributos: set[str]) -> dict[str, dict[str, int]]:
        """Módulo → {atributo → n.º de usos} para los atributos dados.

        Cuenta la llamada (``x.attr()``) Y la referencia enlazada
        (``x.attr`` pasada como callable — el destino de Synthesis viaja así);
        el Attribute que es ``func`` de un Call contado no se double-contabiliza.
        """
        hallados: dict[str, dict[str, int]] = {}
        for py in sorted(raiz.rglob("*.py")):
            if py.name == "path_resolver.py":
                continue
            try:
                arbol = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            conteo: dict[str, int] = {}
            func_ids: set[int] = set()
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute) and nodo.func.attr in atributos:
                    conteo[nodo.func.attr] = conteo.get(nodo.func.attr, 0) + 1
                    func_ids.add(id(nodo.func))
            for nodo in ast.walk(arbol):
                if isinstance(nodo, ast.Attribute) and nodo.attr in atributos and id(nodo) not in func_ids:
                    conteo[nodo.attr] = conteo.get(nodo.attr, 0) + 1
            if conteo:
                hallados[str(py.relative_to(raiz.parents[0])).replace("\\", "/")] = conteo
        return hallados

    def test_cada_consumer_declara_que_raiz_necesita(self) -> None:
        """Igualdad literal de los inventarios por tipo de raíz (no muestreo).

        Un consumer nuevo —o uno existente que cambie de accessor— rompe el
        ancla hasta clasificarlo en el docstring con su tipo de path. La
        definición del accessor (``path_resolver.py``) y los tests no cuentan
        como consumo.
        """
        raiz = pathlib.Path(__file__).resolve().parents[1] / "sky_claw"
        hallados = self._inventario(
            raiz,
            {
                "get_mo2_path",
                "get_mo2_instance_data_root",
                "get_mo2_path_raw",
                "has_explicit_mo2_install_selection",
                "get_mo2_mods_path",
                "get_mo2_mods_path_best_effort",
                "get_mo2_mods_path_para_destino",
            },
        )

        def _por_atributo(atributo: str) -> dict[str, int]:
            return {modulo: conteo[atributo] for modulo, conteo in hallados.items() if atributo in conteo}

        assert _por_atributo("get_mo2_path") == self._INSTALL_O_CAPABILITY, (
            "Cambió el inventario INSTALL/CAPABILITY de get_mo2_path(). Si el "
            "sitio nuevo opera sobre profiles/overwrite/mods, debe usar "
            "get_mo2_instance_data_root()/get_mo2_mods_path() y clasificarse "
            "en el docstring; si es INSTALL real, actualiza el mapa con su racional."
        )
        assert _por_atributo("get_mo2_instance_data_root") == self._INSTANCE_DATA, (
            "Cambió el inventario de get_mo2_instance_data_root(). Clasifica "
            "el sitio nuevo en el docstring antes de que pase CI."
        )
        assert _por_atributo("get_mo2_mods_path") == self._MODS_ESTRICTO, (
            "Cambió el inventario MODS estricto. Todo consumo de mods pasa por "
            "get_mo2_mods_path() (o su severidad best-effort/para-destino), "
            "nunca por <raíz>/mods a mano: mod_directory puede declarar otro árbol."
        )
        assert _por_atributo("get_mo2_mods_path_best_effort") == self._MODS_BEST_EFFORT, (
            "Cambió el inventario MODS best-effort. Un destino de ESCRITURA no "
            "puede usar esta severidad (ocultaría un mod_directory "
            "irresoluble): usa get_mo2_mods_path() o _para_destino."
        )
        assert _por_atributo("get_mo2_mods_path_para_destino") == self._MODS_PARA_DESTINO, (
            "Cambió el inventario MODS-para-destino. Todo destino de escritura "
            "bajo mods debe declararse acá con su severidad fail-closed (B de #555)."
        )
        assert _por_atributo("get_mo2_path_raw") == self._RAW_INSTALL, (
            "Cambió el inventario raw-install. El raw de datos no existe como "
            "representación sin resolver: clasifica el sitio antes de usarlo."
        )
        assert _por_atributo("has_explicit_mo2_install_selection") == self._SENAL_EXPLICITA, (
            "Cambió el inventario de la señal explícita. Todo fallback a "
            "detect_mo2_path() debe consultarla: un hint inválido no habilita "
            "detectar otra instalación."
        )


class TestConsumidoresGetMo2PathConComposicion:
    """Los arquetipos de consumidor de ``get_mo2_path()`` con el fix activo.

    No se mockea el accessor: se arma un ``PathResolutionService`` REAL con
    ``mo2_install_dir`` inyectado y el entorno divergente, y se ejecuta la
    lógica productiva de cada familia. Demostración exigida: devolver la
    instalación inyectada NO activa ninguna operación insegura — los paths
    siguen saliendo por ``PathValidator`` y los gates conservan su semántica
    de fail-closed.
    """

    def test_consumer_mutable_sandbox_de_synthesis_arma_sobre_la_inyectada(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """Consumer MUTABLE: ``build_synthesis_flow_provider`` (dispatcher).

        El sandbox de promoción de Synthesis muta modlist/plugins de la
        instancia: si el provider tomara ``MO2_PATH`` del entorno, armaría el
        sandbox sobre OTRA instalación que la seleccionada. Con el fix arma
        sobre la inyectada aunque el entorno diga otra cosa (y sin env, antes
        del fix fallaba con RuntimeError aunque hubiera composición activa).
        """
        from sky_claw.app.orchestrator.dispatcher_dependencies import (
            build_synthesis_flow_provider,
        )

        selected = tmp_path / "selected"
        selected.mkdir()
        env_install = tmp_path / "wrong-env"
        env_install.mkdir()
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=selected,
        )
        with patch.dict(os.environ, {"MO2_PATH": str(env_install)}, clear=True):
            flow = build_synthesis_flow_provider(
                path_resolver=resolver,
                profile_name="Default",
                hitl_guard=None,
            )()
        assert flow._sandbox._mo2_root == selected.resolve()

    def test_consumer_mutable_sin_nada_configurado_falla_cerrado(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """Consumer MUTABLE, rama fail-closed: sin instalación conocida, error.

        El RuntimeError del provider es el gate que impide armar un sandbox
        sin instalación; el fix no lo afloja.
        """
        from sky_claw.app.orchestrator.dispatcher_dependencies import (
            build_synthesis_flow_provider,
        )

        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=None,
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "sky_claw.app.core.path_resolver._CANDIDATE_MO2_PATHS",
                (str(tmp_path / "no-existe"),),
            ),
            pytest.raises(RuntimeError, match="MO2_PATH must be configured"),
        ):
            build_synthesis_flow_provider(
                path_resolver=resolver,
                profile_name="Default",
                hitl_guard=None,
            )()

    def test_consumer_gate_preflight_de_dyndolod_existe_con_inyectada(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """Consumer GATE: preflight de DynDOLOD se construye con la inyectada.

        Misma familia booleana que ``get_mo2_path() is not None`` en xEdit y
        LOOT: sin env, antes del fix el preflight no existía (None → sin gate
        → ritual sin sensores) aunque la instalación seleccionada estuviera
        allí. Con el fix, el gate existe y opera sobre la instalación
        seleccionada: los sensores que enumera ``mods/`` (``scan_mods_dir``)
        sólo se activan con raíz VALIDADA por el sandbox — propiedad que el
        constructor ``build_vfs_sensor`` conserva (la enumeración exige la
        contraparte validada; el raw crudo es sólo para ``lstat``).
        """
        from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

        selected = tmp_path / "selected"
        selected.mkdir()
        game = tmp_path / "game"
        game.mkdir()
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=selected,
        )
        servicio = DynDOLODPipelineService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            journal=MagicMock(),
            path_resolver=resolver,
            event_bus=MagicMock(),
        )
        with patch.dict(
            os.environ,
            {"SKYRIM_PATH": str(game)},
            clear=True,
        ):
            preflight = servicio._ensure_preflight()
        assert preflight is not None

    def test_consumer_gate_no_se_abre_con_inyectada_invalida(
        self,
        tmp_path: pathlib.Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Consumer GATE, rama de seguridad: hint inválido → sin preflight.

        La propiedad de seguridad del arquetipo gate: un hint que no valida
        contra el sandbox NO abre el gate (el preflight queda ``None``, igual
        que con el accessor legacy), aunque ``get_mo2_path_raw`` sí devuelva
        el path crudo para lstat — la enumeración de ``mods/`` exige la
        contraparte validada y esa sigue siendo ``None``.
        """
        from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

        selected = tmp_path_factory.mktemp("fuera_del_sandbox")
        game = tmp_path / "game"
        game.mkdir()
        resolver = PathResolutionService(
            # Sandbox = solo tmp_path; `selected` queda fuera.
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=selected,
        )
        servicio = DynDOLODPipelineService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            journal=MagicMock(),
            path_resolver=resolver,
            event_bus=MagicMock(),
        )
        with patch.dict(os.environ, {"SKYRIM_PATH": str(game)}, clear=True):
            preflight = servicio._ensure_preflight()
        assert preflight is None

    def test_consumer_readonly_preview_lee_de_la_inyectada_con_fallback_intacto(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """Consumer READ-ONLY: preview de plugins sobre la instalación inyectada.

        ``_plugin_dirs`` es best-effort y read-only (cuenta headers): con el
        fix lee ``mods/``/``overwrite/`` de la instalación seleccionada sin
        ``MO2_PATH``; su fallback a ``detect_mo2_path()`` (contrato propio del
        preview, preexistente) queda intacto para el caso sin composición.
        Cada dir sigue pasando por ``PathValidator`` antes de leerse.
        """
        from sky_claw.app.orchestrator.preview.chain_preview_service import (
            ChainPreviewService,
        )

        selected = tmp_path / "selected"
        # mods/ con un mod de verdad: _plugin_dirs agrega cada mod individual
        # (precedencia del VFS), no la raíz mods/ — más overwrite/ completo.
        mod = selected / "mods" / "Mi Mod"
        mod.mkdir(parents=True)
        (selected / "overwrite").mkdir(parents=True)
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=selected,
        )
        # Construcción mínima (sólo lo que _plugin_dirs lee), patrón __new__
        # de los tests de wiring de supervisor.
        servicio = ChainPreviewService.__new__(ChainPreviewService)
        servicio._path_resolver = resolver
        servicio._path_validator = PathValidator(roots=[tmp_path])

        with patch.dict(os.environ, {}, clear=True):
            dirs = servicio._plugin_dirs()

        assert selected.resolve() / "overwrite" in dirs
        assert mod.resolve() in dirs


class TestRaizDeDatosDeInstanciaSeparada:
    """Contrato install != data (P1): la instalacion y los datos pueden vivir
    en arboles/discos distintos (evidencia documental del rig:
    ``C:\\Modding\\ModOrganizer2`` vs ``G:\\Modding\\MO2\\SkyrimSE``).

    Topologia bajo ``tmp_path`` (C-like vs G-like):

    * ``install/``: ``ModOrganizer.exe`` + ``ModOrganizer.ini`` portable que
      declara ``base_directory=<data>``.
    * ``data/``: ``profiles/Default/modlist.txt``, ``overwrite/`` y ``mods/``.

    Los accessors deben distinguir los cuatro conceptos: INSTALL_ROOT,
    INSTANCE_DATA_ROOT, MODS_DIR y PROFILE/MODLIST.
    """

    @staticmethod
    def _montar_split(
        tmp_path: pathlib.Path,
        *,
        mod_directory: pathlib.Path | None = None,
        con_overwrite: bool = True,
        crear_mods: bool = True,
    ) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
        """Monta install + data separados; devuelve ``(install, data, mods)``.

        ``crear_mods=False`` deja el directorio declarado SIN existir (caso
        fail-closed del B de #555: mod_directory declarado e inexistente).
        """
        install = tmp_path / "install"
        install.mkdir()
        (install / "ModOrganizer.exe").write_bytes(b"fake exe")
        data = tmp_path / "data"
        (data / "profiles" / "Default").mkdir(parents=True)
        (data / "profiles" / "Default" / "modlist.txt").write_text("a", encoding="utf-8")
        if con_overwrite:
            (data / "overwrite").mkdir(parents=True)
        mods = mod_directory if mod_directory is not None else data / "mods"
        if crear_mods:
            mods.mkdir(parents=True)
        (install / "ModOrganizer.ini").write_text(
            _texto_ini_mo2(
                base_directory=_formato_qt(data),
                mod_directory=_formato_qt(mods) if mod_directory is not None else None,
            ),
            encoding="utf-8",
        )
        return install, data, mods

    @staticmethod
    def _resolver_con_hint(
        tmp_path: pathlib.Path,
        install: pathlib.Path,
    ) -> PathResolutionService:
        return PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=install,
        )

    def test_los_cuatro_conceptos_se_distinguen(self, tmp_path: pathlib.Path) -> None:
        """INSTALL, DATA, MODS y MODLIST apuntan cada uno a su arbol."""
        install, data, mods = self._montar_split(tmp_path)
        resolver = self._resolver_con_hint(tmp_path, install)
        with patch.dict(os.environ, {}, clear=True):
            ruta_install = resolver.get_mo2_path()
            ruta_datos = resolver.get_mo2_instance_data_root()
            ruta_mods = resolver.get_mo2_mods_path()
            ruta_modlist = resolver.resolve_modlist_path("Default")
        assert ruta_install is not None and _mismo_path(ruta_install, install)
        assert ruta_datos is not None and _mismo_path(ruta_datos, data)
        assert _mismo_path(ruta_mods, mods)
        assert _mismo_path(ruta_modlist, data / "profiles" / "Default" / "modlist.txt")
        # La trampa anti-portable: nada de datos cuelga de install/.
        assert not _mismo_path(ruta_datos, install)
        assert not str(ruta_modlist).startswith(str(install.resolve()))

    def test_sandbox_de_synthesis_usa_data_root_no_install(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """P1: ``ProfileSandbox`` clona profiles/overwrite de DATOS, no de install.

        Con el comportamiento viejo el provider pasaba ``get_mo2_path()``
        (install) y el sandbox clonaba ``<install>/profiles`` (inexistente →
        ``ProfileNotFoundError`` o arbol rancio).
        """
        from sky_claw.app.orchestrator.dispatcher_dependencies import (
            build_synthesis_flow_provider,
        )

        install, data, _mods = self._montar_split(tmp_path)
        resolver = self._resolver_con_hint(tmp_path, install)
        with patch.dict(os.environ, {}, clear=True):
            flow = build_synthesis_flow_provider(
                path_resolver=resolver,
                profile_name="Default",
                hitl_guard=None,
            )()
        assert _mismo_path(flow._sandbox._mo2_root, data)
        assert not _mismo_path(flow._sandbox._mo2_root, install)

    def test_phantom_hint_no_abre_capability_gates(self, tmp_path: pathlib.Path) -> None:
        """P2: hint dentro del sandbox pero inexistente → ``None``."""
        fantasma = tmp_path / "install_fantasma"
        resolver = self._resolver_con_hint(tmp_path, fantasma)
        with patch.dict(os.environ, {}, clear=True):
            assert resolver.get_mo2_path() is None
            assert resolver.get_mo2_instance_data_root() is None
            assert resolver.has_explicit_mo2_install_selection() is True

    def test_hint_archivo_falla_cerrado(self, tmp_path: pathlib.Path) -> None:
        """P2: hint que es un archivo (no un directorio) → ``None``."""
        archivo = tmp_path / "no_es_install.exe"
        archivo.write_bytes(b"fake")
        resolver = self._resolver_con_hint(tmp_path, archivo)
        with patch.dict(os.environ, {}, clear=True):
            assert resolver.get_mo2_path() is None
            assert resolver.get_mo2_instance_data_root() is None

    def test_has_explicit_distinque_no_hint_de_hint(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """La senal explicita: sin hint es False, con hint (valido o no) es True."""
        install, _data, _mods = self._montar_split(tmp_path)
        sin_hint = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=None,
        )
        con_hint = self._resolver_con_hint(tmp_path, install)
        con_hint_fantasma = self._resolver_con_hint(tmp_path, tmp_path / "fantasma")
        assert sin_hint.has_explicit_mo2_install_selection() is False
        assert con_hint.has_explicit_mo2_install_selection() is True
        assert con_hint_fantasma.has_explicit_mo2_install_selection() is True

    def test_hint_invalido_no_degrada_a_otra_instalacion_detectada(
        self,
        tmp_path: pathlib.Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """P2: hint A invalido + instalacion B detectable → B NO se usa.

        ``ChainPreviewService._plugin_dirs`` interpretaba ``None`` como "nada
        configurado" y llamaba a ``detect_mo2_path()`` (caso prohibido:
        seleccion explicita A invalidada ≠ via libre para detectar B).
        """
        from sky_claw.app.orchestrator.preview.chain_preview_service import (
            ChainPreviewService,
        )

        instalacion_b = tmp_path / "install_b"
        instalacion_b.mkdir()
        (instalacion_b / "ModOrganizer.exe").write_bytes(b"fake exe")
        mod_b = instalacion_b / "mods" / "ModB"
        mod_b.mkdir(parents=True)
        (instalacion_b / "overwrite").mkdir(parents=True)
        hint_a = tmp_path_factory.mktemp("hint_a_fuera_del_sandbox")
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=hint_a,
        )
        servicio = ChainPreviewService.__new__(ChainPreviewService)
        servicio._path_resolver = resolver
        servicio._path_validator = PathValidator(roots=[tmp_path])

        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "sky_claw.app.core.path_resolver._CANDIDATE_MO2_PATHS",
                (str(instalacion_b),),
            ),
        ):
            assert resolver.get_mo2_path() is None
            dirs = servicio._plugin_dirs()

        assert dirs == ()
        assert mod_b.resolve() not in dirs

    def test_loot_preflight_con_hint_invalido_no_llama_a_detect(
        self,
        tmp_path: pathlib.Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """P2: el fallback de LOOT a ``detect_mo2_path`` respeta el hint invalido.

        Cuando el raw es ``None`` (hint sanitizado a nada) el preflight caia a
        auto-deteccion aunque hubiera seleccion explicita. Con el guard, un
        hint presente pero invalido nunca dispara la deteccion de otro arbol.
        """
        from sky_claw.local.tools.loot_service import LootSortingService

        hint_a = tmp_path_factory.mktemp("hint_a_loot_fuera")
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=hint_a,
        )
        servicio = LootSortingService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            path_resolver=resolver,
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(resolver, "get_mo2_path_raw", return_value=None),
            patch.object(
                resolver,
                "detect_mo2_path",
                side_effect=AssertionError("no debe auto-detectar con hint explicito"),
            ),
        ):
            servicio._ensure_preflight()

    def test_raw_hint_con_nul_falla_cerrado(self, tmp_path: pathlib.Path) -> None:
        """P2: hint con NUL embebido → raw ``None``, sin ``ValueError`` al consumer."""
        hint_con_nul = pathlib.Path(str(tmp_path) + "\x00hint_roto")
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=hint_con_nul,
        )
        with patch.dict(os.environ, {}, clear=True):
            assert resolver.get_mo2_path_raw() is None
            assert resolver.get_mo2_path() is None

    def test_synthesis_output_usa_data_root_y_respeta_mod_directory(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """Synthesis escribe en el arbol de DATOS y respeta ``mod_directory``.

        Sin ``overwrite/`` en disco y con ``mod_directory`` fuera de
        ``<data>/mods``, el destino es ``<custom>/Synthesis Output`` — nunca
        ``<install>/mods/Synthesis Output``. Cubre el segundo caso con overwrite
        presente (destino = overwrite de datos).
        """
        from sky_claw.local.tools.synthesis_service import SynthesisPipelineService

        mods_custom = tmp_path / "ModsPersonales"
        install, data, _mods = self._montar_split(
            tmp_path,
            mod_directory=mods_custom,
            con_overwrite=False,
        )
        juego = tmp_path / "game"
        juego.mkdir()
        exe = tmp_path / "Synthesis.exe"
        exe.write_bytes(b"fake")
        resolver = self._resolver_con_hint(tmp_path, install)
        servicio = SynthesisPipelineService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            journal=MagicMock(),
            path_resolver=resolver,
            event_bus=MagicMock(),
            pipeline_config_path=tmp_path / "pipeline.json",
        )
        with patch.dict(
            os.environ,
            {"SKYRIM_PATH": str(juego), "SYNTHESIS_EXE": str(exe)},
            clear=True,
        ):
            runner = servicio._ensure_synthesis_runner()
        assert _mismo_path(runner._config.output_path, mods_custom / "Synthesis Output")
        assert not str(runner._config.output_path.resolve()).startswith(str(install.resolve()))

        (data / "overwrite").mkdir()
        with patch.dict(
            os.environ,
            {"SKYRIM_PATH": str(juego), "SYNTHESIS_EXE": str(exe)},
            clear=True,
        ):
            servicio2 = SynthesisPipelineService(
                lock_manager=MagicMock(),
                snapshot_manager=MagicMock(),
                journal=MagicMock(),
                path_resolver=resolver,
                event_bus=MagicMock(),
                pipeline_config_path=tmp_path / "pipeline.json",
            )
            runner2 = servicio2._ensure_synthesis_runner()
        assert _mismo_path(runner2._config.output_path, data / "overwrite")

    def test_loot_load_order_resuelve_desde_data(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """LOOT lee el load order del perfil en DATOS, no en install."""
        from sky_claw.local.tools.loot_service import LootSortingService

        install, data, _mods = self._montar_split(tmp_path)
        (data / "profiles" / "Default" / "plugins.txt").write_text(
            "*Skyrim.esm\n",
            encoding="utf-8",
        )
        resolver = self._resolver_con_hint(tmp_path, install)
        servicio = LootSortingService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            path_resolver=resolver,
        )
        with patch.dict(os.environ, {}, clear=True):
            archivos = servicio._ensure_load_order_resolver().resolve().files
        assert archivos, "el plugins.txt de datos debio resolverse"
        assert all(data.resolve() in a.parents or a == data.resolve() for a in archivos)
        assert not any(install.resolve() in a.parents for a in archivos)

    def test_preflights_reciben_data_root(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        """DynDOLOD/Wrye/Synthesis/Pandora arman sus sensores sobre DATOS.

        Espia el builder compartido de fuentes del perfil y captura el ``mo2``
        que cada preflight le pasa: debe ser la raiz de datos, nunca install.
        """
        import sky_claw.local.validators.preflight_sensors as sensores
        from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
        from sky_claw.local.tools.pandora_service import PandoraPipelineService
        from sky_claw.local.tools.synthesis_service import SynthesisPipelineService
        from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService

        install, data, _mods = self._montar_split(tmp_path)
        juego = tmp_path / "game"
        juego.mkdir()
        (data / "profiles" / "Default" / "plugins.txt").write_text(
            "*Skyrim.esm\n",
            encoding="utf-8",
        )
        capturados: list[pathlib.Path] = []
        original = sensores.build_mo2_profile_sources_resolver

        def _espia(*, game, mo2, profile, **extra):  # type: ignore[no-untyped-def]
            capturados.append(mo2)
            return original(game=game, mo2=mo2, profile=profile, **extra)

        resolver = self._resolver_con_hint(tmp_path, install)
        servicios = [
            DynDOLODPipelineService(
                lock_manager=MagicMock(),
                snapshot_manager=MagicMock(),
                journal=MagicMock(),
                path_resolver=resolver,
                event_bus=MagicMock(),
            ),
            WryeBashPipelineService(
                lock_manager=MagicMock(),
                snapshot_manager=MagicMock(),
                path_resolver=resolver,
            ),
            SynthesisPipelineService(
                lock_manager=MagicMock(),
                snapshot_manager=MagicMock(),
                journal=MagicMock(),
                path_resolver=resolver,
                event_bus=MagicMock(),
                pipeline_config_path=tmp_path / "pipeline.json",
            ),
            PandoraPipelineService(
                lock_manager=MagicMock(),
                snapshot_manager=MagicMock(),
                path_resolver=resolver,
            ),
        ]
        with (
            patch.dict(os.environ, {"SKYRIM_PATH": str(juego)}, clear=True),
            patch.object(
                sensores,
                "build_mo2_profile_sources_resolver",
                side_effect=_espia,
            ),
        ):
            for servicio in servicios:
                servicio._ensure_preflight()

        assert len(capturados) >= 4, f"los preflights debieron consultar fuentes: {capturados}"
        for mo2_usado in capturados:
            assert _mismo_path(mo2_usado, data), f"preflight sobre install: {mo2_usado}"
            assert not _mismo_path(mo2_usado, install)


class TestModsDirSeparadoEnConsumidoresDeDatos:
    """P1 (ronda 2 de #555): ``instance_data_root / "mods"`` NO es una
    abstraccion del MODS_DIR.

    ``[Settings] mod_directory`` puede declarar los mods fuera de
    ``<base_directory>/mods`` (contrato PathSettings, mismo que ya cubre
    ``get_mo2_mods_path``). Cada primitiva de datos (sources resolver,
    sensor VFS, preview, LOOT) debe recibir POR SEPARADO la raiz de datos
    (profiles/overwrite) y el MODS_DIR declarado; nadie puede reconstruir
    ``<datos>/mods`` a mano cuando existe la declaracion.

    Topologia adversarial: install != data != mods (los tres en arboles
    distintos bajo ``tmp_path``).
    """

    @staticmethod
    def _montar(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
        """install != data; ``mod_directory`` apunta a ModsPersonales."""
        mods_custom = tmp_path / "ModsPersonales"
        install, data, mods = TestRaizDeDatosDeInstanciaSeparada._montar_split(
            tmp_path,
            mod_directory=mods_custom,
        )
        (mods_custom / "UnMod").mkdir(parents=True)
        return install, data, mods

    @staticmethod
    def _resolver(tmp_path: pathlib.Path, install: pathlib.Path) -> PathResolutionService:
        return PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=install,
        )

    def test_preflights_reciben_mods_dir_declarado_no_el_default(self, tmp_path: pathlib.Path) -> None:
        """Sources/VFS de los 4 preflights: mods = mod_directory, datos = data root."""
        import sky_claw.local.validators.preflight_sensors as sensores
        from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService
        from sky_claw.local.tools.pandora_service import PandoraPipelineService
        from sky_claw.local.tools.synthesis_service import SynthesisPipelineService
        from sky_claw.local.tools.wrye_bash_service import WryeBashPipelineService

        install, data, mods = self._montar(tmp_path)
        juego = tmp_path / "game"
        (juego / "Data").mkdir(parents=True)
        capturados_prof: list[tuple[pathlib.Path | None, pathlib.Path | None]] = []
        capturados_vfs: list[tuple[pathlib.Path | None, pathlib.Path | None]] = []
        original_prof = sensores.build_mo2_profile_sources_resolver
        original_vfs = sensores.build_vfs_sensor

        def _esp_prof(*, game, mo2, profile, **extra):  # type: ignore[no-untyped-def]
            capturados_prof.append((mo2, extra.get("mods_dir")))
            return original_prof(game=game, mo2=mo2, profile=profile, **extra)

        def _esp_vfs(**kw):  # type: ignore[no-untyped-def]
            capturados_vfs.append((kw.get("raw_mo2"), kw.get("mods_dir")))
            return original_vfs(**kw)

        resolver = self._resolver(tmp_path, install)
        servicios = [
            DynDOLODPipelineService(
                lock_manager=MagicMock(),
                snapshot_manager=MagicMock(),
                journal=MagicMock(),
                path_resolver=resolver,
                event_bus=MagicMock(),
            ),
            WryeBashPipelineService(
                lock_manager=MagicMock(),
                snapshot_manager=MagicMock(),
                path_resolver=resolver,
            ),
            SynthesisPipelineService(
                lock_manager=MagicMock(),
                snapshot_manager=MagicMock(),
                journal=MagicMock(),
                path_resolver=resolver,
                event_bus=MagicMock(),
                pipeline_config_path=tmp_path / "pipeline.json",
            ),
            PandoraPipelineService(
                lock_manager=MagicMock(),
                snapshot_manager=MagicMock(),
                path_resolver=resolver,
            ),
        ]
        with (
            patch.dict(os.environ, {"SKYRIM_PATH": str(juego)}, clear=True),
            patch.object(sensores, "build_mo2_profile_sources_resolver", side_effect=_esp_prof),
            patch.object(sensores, "build_vfs_sensor", side_effect=_esp_vfs),
        ):
            for servicio in servicios:
                servicio._ensure_preflight()

        assert capturados_prof and capturados_vfs
        for mo2_usado, mods_usado in capturados_prof:
            assert mo2_usado is not None and _mismo_path(mo2_usado, data)
            assert mods_usado is not None and _mismo_path(mods_usado, mods), (
                f"mods reconstruido desde la raiz de datos: {mods_usado}"
            )
        for raw_mo2, mods_dir in capturados_vfs:
            assert raw_mo2 is not None and _mismo_path(raw_mo2, data)
            assert mods_dir is not None and _mismo_path(mods_dir, mods), (
                f"scan de mods sobre el default <datos>/mods: {mods_dir}"
            )

    def test_loot_y_preview_escanean_el_mods_declarado(self, tmp_path: pathlib.Path) -> None:
        """LOOT (sources resolver) y ChainPreview: mods = mod_directory; overwrite = datos."""
        import sky_claw.local.mo2.plugin_sources as psrc
        from sky_claw.app.orchestrator.preview.chain_preview_service import ChainPreviewService
        from sky_claw.local.tools.loot_service import LootSortingService

        install, data, mods = self._montar(tmp_path)
        juego = tmp_path / "game"
        (juego / "Data").mkdir(parents=True)
        capturados: list[dict[str, pathlib.Path | None]] = []
        original = psrc.resolve_plugin_sources

        def _esp(**kw):  # type: ignore[no-untyped-def]
            capturados.append(kw)
            return original(**kw)

        resolver = self._resolver(tmp_path, install)
        loot = LootSortingService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            path_resolver=resolver,
        )
        preview = ChainPreviewService.__new__(ChainPreviewService)
        preview._path_resolver = resolver
        preview._path_validator = PathValidator(roots=[tmp_path])
        with (
            patch.dict(os.environ, {"SKYRIM_PATH": str(juego)}, clear=True),
            patch.object(psrc, "resolve_plugin_sources", _esp),
        ):
            loot._ensure_preflight()
            preview._plugin_dirs()

        assert capturados, "ni LOOT ni el preview resolvieron fuentes de plugins"
        for kw in capturados:
            mods_usado = kw["mo2_mods_dir"]
            assert mods_usado is not None and _mismo_path(mods_usado, mods), f"mods reconstruido a mano: {mods_usado}"
            overwrite_usado = kw["mo2_overwrite_dir"]
            if overwrite_usado is not None:
                assert _mismo_path(overwrite_usado, data / "overwrite")


class TestSynthesisDestinoFailClosed:
    """P2 (ronda 2 de #555): el destino de escritura no puede inventar
    ``<datos>/mods`` cuando la instancia DECLARO otro ``mod_directory`` y ese
    directorio no es resoluble.

    Sin ``overwrite/`` en disco y con el MODS_DIR declarado inexistente, el
    runner debe ABORTAR con evidencia (mismo espiritu que ``t5b`` del
    resolver: degradar en silencio contra la evidencia del INI es el defecto
    que la serie #552/#554/este PR cierra). El fallback a ``<datos>/mods``
    solo es legitimo sin metadata (portable puro, donde Synthesis crea el
    arbol).
    """

    def test_mod_directory_inexistente_sin_overwrite_aborta_con_evidencia(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        from sky_claw.local.tools.synthesis_service import SynthesisPipelineService

        mods_roto = tmp_path / "ModsDeclaradosInexistentes"  # NO se crea
        install, data, _mods = TestRaizDeDatosDeInstanciaSeparada._montar_split(
            tmp_path,
            mod_directory=mods_roto,
            con_overwrite=False,
            crear_mods=False,
        )
        juego = tmp_path / "game"
        juego.mkdir()
        exe = tmp_path / "Synthesis.exe"
        exe.write_bytes(b"fake")
        resolver = PathResolutionService(
            path_validator=PathValidator(roots=[tmp_path]),
            profile_name="Default",
            mo2_install_dir=install,
        )
        servicio = SynthesisPipelineService(
            lock_manager=MagicMock(),
            snapshot_manager=MagicMock(),
            journal=MagicMock(),
            path_resolver=resolver,
            event_bus=MagicMock(),
            pipeline_config_path=tmp_path / "pipeline.json",
        )
        with (
            patch.dict(
                os.environ,
                {"SKYRIM_PATH": str(juego), "SYNTHESIS_EXE": str(exe)},
                clear=True,
            ),
            pytest.raises(RuntimeError, match="declara su directorio de mods"),
        ):
            servicio._ensure_synthesis_runner()
        # No se fabrico el destino historico sobre <datos>/mods.
        assert not (data / "mods" / "Synthesis Output").exists()
        assert not (data / "mods").exists()
