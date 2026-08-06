"""El snapshot no puede declarar SKSE "encontrado" con una instalación que no carga.

SKSE no es un tool que Sky-Claw ejecute desde donde esté (como LOOT o xEdit): es un
runtime que el JUEGO carga, y solo carga si el loader y el DLL de runtime del build
exacto del ejecutable están en la raíz de Skyrim. El resolver genérico
(`_resolve_tool_path`) acepta cualquier loader que aparezca en MO2, en la carpeta de
tools, en el PATH o pinneado por config: con eso el snapshot reporta ✅ para un loader
suelto en otra carpeta o para un upgrade que quedó a mitad de camino (loader sí, DLL
no), y el usuario se entera recién cuando arranca el juego sin SKSE.
"""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.local.discovery import scanner as scanner_mod
from sky_claw.local.discovery.environment import SkyrimEdition
from sky_claw.local.discovery.scanner import EnvironmentScanner, find_skse_installation


def _skyrim_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Raíz de juego mínima: solo el ejecutable, que es lo que mira `_find_skyrim`."""
    game = tmp_path / "Skyrim Special Edition"
    game.mkdir(parents=True, exist_ok=True)
    (game / "SkyrimSE.exe").write_bytes(b"MZ")
    return game


def _instalar_skse(game_dir: pathlib.Path, *, loader: str, dll: str | None) -> None:
    (game_dir / loader).write_bytes(b"MZ")
    if dll is not None:
        (game_dir / dll).write_bytes(b"MZ")


class TestFindSkseInstallation:
    """El detector puntual, sin el resto del scan alrededor."""

    def test_instalacion_completa_devuelve_el_loader(self, tmp_path: pathlib.Path) -> None:
        game = _skyrim_dir(tmp_path)
        _instalar_skse(game, loader="skse64_loader.exe", dll="skse64_1_6_1170.dll")

        assert find_skse_installation(game, edition=SkyrimEdition.AE, game_version="1.6.1170") == (
            game / "skse64_loader.exe"
        )

    def test_loader_sin_dll_de_runtime_no_cuenta(self, tmp_path: pathlib.Path) -> None:
        """El caso "dirty upgrade": la limpieza borró el DLL viejo y la copia falló."""
        game = _skyrim_dir(tmp_path)
        _instalar_skse(game, loader="skse64_loader.exe", dll=None)

        assert find_skse_installation(game, edition=SkyrimEdition.AE, game_version="1.6.1170") is None

    def test_dll_de_otro_build_no_cuenta(self, tmp_path: pathlib.Path) -> None:
        """`skse64_1_5_97.dll` sobre un runtime 1.6.1170 no carga: no es una instalación."""
        game = _skyrim_dir(tmp_path)
        _instalar_skse(game, loader="skse64_loader.exe", dll="skse64_1_5_97.dll")

        assert find_skse_installation(game, edition=SkyrimEdition.AE, game_version="1.6.1170") is None

    def test_el_steam_loader_no_es_el_dll_de_runtime(self, tmp_path: pathlib.Path) -> None:
        """`skse*_steam_loader.dll` matchea el glob `skse*.dll` pero no codifica versión."""
        game = _skyrim_dir(tmp_path)
        _instalar_skse(game, loader="skse64_loader.exe", dll="skse64_steam_loader.dll")

        assert find_skse_installation(game, edition=SkyrimEdition.AE, game_version="1.6.1170") is None

    def test_loader_de_otra_familia_no_sirve_para_la_edicion(self, tmp_path: pathlib.Path) -> None:
        """LE usa `skse_loader.exe`; SE/AE usan `skse64_loader.exe`. No son intercambiables."""
        game = _skyrim_dir(tmp_path)
        _instalar_skse(game, loader="skse_loader.exe", dll="skse_1_9_32.dll")

        assert find_skse_installation(game, edition=SkyrimEdition.AE, game_version="1.6.1170") is None
        assert find_skse_installation(game, edition=SkyrimEdition.LE, game_version="1.9.32") == (
            game / "skse_loader.exe"
        )

    def test_sin_version_detectada_no_exige_build_exacto(self, tmp_path: pathlib.Path) -> None:
        """Sin `pefile` la versión sale vacía: seguir exigiendo loader + DLL, no el build.

        Degradar a "no hay SKSE" ahí reportaría faltante una instalación buena en toda
        máquina sin pefile; exigir loader + runtime DLL sigue atajando el upgrade a medias.
        """
        game = _skyrim_dir(tmp_path)
        _instalar_skse(game, loader="skse64_loader.exe", dll="skse64_1_6_640.dll")

        assert find_skse_installation(game, edition=SkyrimEdition.AE, game_version="") == (game / "skse64_loader.exe")

    def test_edicion_desconocida_acepta_cualquier_familia(self, tmp_path: pathlib.Path) -> None:
        game = _skyrim_dir(tmp_path)
        _instalar_skse(game, loader="skse_loader.exe", dll="skse_1_9_32.dll")

        assert find_skse_installation(game, edition=SkyrimEdition.UNKNOWN, game_version="") == (
            game / "skse_loader.exe"
        )


class TestSnapshot:
    """El mismo recorte, visto desde `EnvironmentScanner.scan()`."""

    @pytest.fixture(autouse=True)
    def _version_determinista(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """El stub `b"MZ"` no tiene recurso PE: fijar edición/versión para no depender de la heurística."""
        monkeypatch.setattr(
            scanner_mod,
            "_detect_skyrim_version",
            lambda _exe: ("1.6.1170", SkyrimEdition.AE),
        )

    async def _scan(self, game: pathlib.Path, **kwargs: object) -> object:
        scanner = EnvironmentScanner(skyrim_path=game, **kwargs)  # type: ignore[arg-type]
        return await scanner.scan()

    async def test_instalacion_completa_se_reporta_presente(self, tmp_path: pathlib.Path) -> None:
        game = _skyrim_dir(tmp_path)
        _instalar_skse(game, loader="skse64_loader.exe", dll="skse64_1_6_1170.dll")

        snap = await self._scan(game)

        assert "skse" in snap.tools  # type: ignore[attr-defined]
        assert snap.tools["skse"].exe_path == game / "skse64_loader.exe"  # type: ignore[attr-defined]
        assert not any(m.technical_name.startswith("skse") for m in snap.missing)  # type: ignore[attr-defined]

    async def test_loader_sin_dll_se_reporta_faltante(self, tmp_path: pathlib.Path) -> None:
        game = _skyrim_dir(tmp_path)
        _instalar_skse(game, loader="skse64_loader.exe", dll=None)

        snap = await self._scan(game)

        assert "skse" not in snap.tools  # type: ignore[attr-defined]
        assert any(m.technical_name.startswith("skse") for m in snap.missing)  # type: ignore[attr-defined]

    async def test_loader_fuera_de_la_raiz_del_juego_no_cuenta(self, tmp_path: pathlib.Path) -> None:
        """Un loader pinneado en config, o suelto en la carpeta de tools, no instala SKSE.

        SKSE se carga por ruta fija junto al ejecutable del juego: un loader en otra
        carpeta puede ejecutarse a mano y no hace que Skyrim cargue los plugins.
        """
        game = _skyrim_dir(tmp_path)
        suelto = tmp_path / "tools" / "SKSE"
        suelto.mkdir(parents=True)
        _instalar_skse(suelto, loader="skse64_loader.exe", dll="skse64_1_6_1170.dll")

        snap = await self._scan(game, tool_paths={"skse": str(suelto / "skse64_loader.exe")})

        assert "skse" not in snap.tools  # type: ignore[attr-defined]
        assert any(m.technical_name.startswith("skse") for m in snap.missing)  # type: ignore[attr-defined]

    async def test_dll_de_otro_build_se_reporta_faltante(self, tmp_path: pathlib.Path) -> None:
        game = _skyrim_dir(tmp_path)
        _instalar_skse(game, loader="skse64_loader.exe", dll="skse64_1_5_97.dll")

        snap = await self._scan(game)

        assert "skse" not in snap.tools  # type: ignore[attr-defined]

    async def test_sin_skyrim_detectado_skse_es_faltante_no_crash(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con `tool_paths` pinneados el scan sigue aunque no haya juego: SKSE no puede resolverse ahí."""

        # Sin esto, el auto-detect real (registro de Windows, Steam library
        # folders, rutas comunes) puede encontrar una instalación real en la
        # máquina que corre el test — como esta — y romper la premisa "sin
        # Skyrim detectado" (mismo guard que ya usa
        # test_environment_scanner_config.py::test_bare_scanner_without_skyrim_stays_critical).
        async def mock_find_skyrim(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(EnvironmentScanner, "_find_skyrim", mock_find_skyrim)

        loot = tmp_path / "loot.exe"
        loot.write_bytes(b"MZ")
        scanner = EnvironmentScanner(tool_paths={"loot": str(loot)})

        snap = await scanner.scan()

        assert "skse" not in snap.tools
        assert any(m.technical_name.startswith("skse") for m in snap.missing)
