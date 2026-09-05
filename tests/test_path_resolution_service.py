"""Tests para PathResolutionService — resolución stateless de rutas MO2/Skyrim.

Verifica EAFP anti-TOCTOU, validación con PathValidator (CRIT-003),
y la interfaz Protocol PathResolver.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from sky_claw.app.core.path_resolver import (
    PathResolutionService,
    PathResolver,
    resolver_mods_dir_de_instancia_mo2,
)
from sky_claw.app.security.path_validator import PathValidator

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
    literal de ruta absoluta (drive de Windows o UNC). Los fixtures escriben
    bajo ``tmp_path`` (variable, no literal); un test nuevo que cree
    accidentalmente ``<volumen>\\Sky-Claw`` o toque la instancia MO2 del
    operador rompe acá. Literales de drive en asserts o patches (solo lectura)
    siguen permitidos.
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

    def test_no_hay_literales_de_drive_en_operaciones_mutantes(self) -> None:
        """Las escrituras nunca reciben rutas absolutas literales del operador."""
        texto = pathlib.Path(__file__).read_text(encoding="utf-8")
        arbol = ast.parse(texto)
        violaciones: list[str] = []
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Call):
                continue
            func = nodo.func
            if isinstance(func, ast.Attribute) and func.attr not in self._MUTADORES:
                continue
            if isinstance(func, ast.Name) and func.id not in self._MUTADORES:
                continue
            args = list(nodo.args) + [kw.value for kw in nodo.keywords if kw.arg == "path"]
            for arg in args:
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and (self._DRIVE_RE.match(arg.value) or self._UNC_RE.match(arg.value))
                ):
                    violaciones.append(
                        f"{func.attr if isinstance(func, ast.Attribute) else func.id}(...{arg.value!r}...)"
                    )
        assert violaciones == [], (
            f"Operaciones mutantes con ruta absoluta literal: {violaciones}. Los tests solo escriben bajo tmp_path."
        )

    def test_los_docstrings_citan_el_rig_como_evidencia(self) -> None:
        """El layout real del bug se nombra solo como evidencia documental."""
        texto = pathlib.Path(__file__).read_text(encoding="utf-8")
        # En el fuente los backslashes van escapados (C:\\Modding\\...).
        assert "C:\\\\Modding\\\\ModOrganizer2" in texto
        assert "G:\\\\Modding\\\\MO2\\\\SkyrimSE" in texto


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
        # El propio resolver conserva el legacy portable (sujeto de este PR):
        # se alcanza SOLO cuando no hay metadata de instancia (o por deferencia
        # ante MO2_PATH explícito de datos).
        "sky_claw/app/core/path_resolver.py": (522, 728),
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
        # Preflight/preview/checkers read-only sobre mo2 raw/validado.
        "sky_claw/local/tools/loot_service.py": (461,),
        "sky_claw/local/validators/vfs_health.py": (123,),
        "sky_claw/local/validators/preflight_sensors.py": (164,),
        "sky_claw/app/orchestrator/preview/chain_preview_service.py": (312,),
        # Rollback/move-aside y staging de DynDOLOD bajo el árbol del broker.
        "sky_claw/app_context.py": (1229,),
        "sky_claw/local/tools/rollback_reconciler.py": (236,),
        "sky_claw/local/tools/output_targets.py": (144,),
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
