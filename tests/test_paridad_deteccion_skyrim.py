"""Los dos detectores de Skyrim tienen que reconocer las mismas instalaciones.

El repo tiene dos: `EnvironmentScanner._find_skyrim` (alimenta el snapshot, y de ahí
sale la carpeta que `run_ritual_install` le pasa a `ensure_skse`) y
`AutoDetector.find_skyrim` (alimenta `local_cfg.skyrim_path`, y de ahí sale la raíz
del juego que `AppContext.start_full` mete en el sandbox del `PathValidator`).

La propiedad que los ata no es de estilo, es de mecanismo: **toda raíz de juego que el
scanner puede entregarle al instalador tiene que estar en el sandbox**. Si el scanner
reconoce una fuente que el AutoDetector no —era el caso de LE por registro, LE por
librería de Steam y GOG—, entonces `local_cfg.skyrim_path` queda vacío, la raíz nunca
entra al validator, y el "Instalar SKSE" de la GUI muere con `PathViolationError` en la
primera línea de `ensure_skse`, DESPUÉS de haber consumido la aprobación del operador.

Los casos se enumeran por fuente de detección, no se muestrean: agregar una fuente al
scanner sin agregarla al AutoDetector tiene que romper acá.
"""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.local import auto_detect as auto_mod
from sky_claw.local.discovery import scanner as scanner_mod
from sky_claw.local.discovery.scanner import EnvironmentScanner

_REG_SSE = r"SOFTWARE\Bethesda Softworks\Skyrim Special Edition"
_REG_SSE_WOW = r"SOFTWARE\WOW6432Node\Bethesda Softworks\Skyrim Special Edition"
_REG_LE = r"SOFTWARE\Bethesda Softworks\Skyrim"
_REG_GOG = r"SOFTWARE\WOW6432Node\GOG.com\Games\1711230643"


def _crear_juego(raiz: pathlib.Path, exe: str) -> pathlib.Path:
    raiz.mkdir(parents=True, exist_ok=True)
    (raiz / exe).write_bytes(b"MZ")
    return raiz


def _sin_fuentes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apaga TODAS las fuentes de ambos detectores; cada caso reenciende la suya."""
    monkeypatch.setattr(scanner_mod, "_reg_read", lambda *_a, **_k: None)
    monkeypatch.setattr(scanner_mod, "_parse_steam_libraries", list)
    monkeypatch.setattr(scanner_mod, "SKYRIM_COMMON_PATHS", ())
    monkeypatch.setattr(auto_mod, "_read_registry_value", lambda *_a, **_k: None)
    monkeypatch.setattr(auto_mod, "_parse_steam_library_folders", lambda _p: [])
    monkeypatch.setattr(auto_mod, "_SKYRIM_COMMON", ())


def _fijar_registro(monkeypatch: pytest.MonkeyPatch, entradas: dict[str, str]) -> None:
    """Ambos detectores leen el registro por helpers distintos con la misma firma."""

    def _leer(_hive: int, subkey: str, _value: str) -> str | None:
        return entradas.get(subkey)

    monkeypatch.setattr(scanner_mod, "_reg_read", _leer)
    monkeypatch.setattr(auto_mod, "_read_registry_value", _leer)


def _fijar_librerias_steam(monkeypatch: pytest.MonkeyPatch, libs: list[pathlib.Path]) -> None:
    monkeypatch.setattr(scanner_mod, "_parse_steam_libraries", lambda: list(libs))
    monkeypatch.setattr(auto_mod, "_parse_steam_library_folders", lambda _p: list(libs))
    # El AutoDetector recorre `_STEAM_DEFAULT_PATHS` para armar la ruta del .vdf; con la
    # tupla vacía nunca llamaría al parser mockeado.
    monkeypatch.setattr(auto_mod, "_STEAM_DEFAULT_PATHS", (r"C:\Steam",))


async def _detectan_ambos(juego: pathlib.Path) -> tuple[pathlib.Path | None, pathlib.Path | None]:
    del_scanner = await EnvironmentScanner()._find_skyrim()
    del_auto = auto_mod.AutoDetector._find_skyrim_inner()
    return del_scanner, del_auto


class TestParidadPorFuente:
    @pytest.fixture(autouse=True)
    def _apagar_fuentes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _sin_fuentes(monkeypatch)

    async def test_registro_sse(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        juego = _crear_juego(tmp_path / "SSE", "SkyrimSE.exe")
        _fijar_registro(monkeypatch, {_REG_SSE: str(juego)})

        assert await _detectan_ambos(juego) == (juego, juego)

    async def test_registro_sse_wow6432node(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """La vista de 32 bits del registro: el scanner ya la miraba, el AutoDetector no."""
        juego = _crear_juego(tmp_path / "SSE", "SkyrimSE.exe")
        _fijar_registro(monkeypatch, {_REG_SSE_WOW: str(juego)})

        assert await _detectan_ambos(juego) == (juego, juego)

    async def test_registro_legendary_edition(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """LE es una de las tres ediciones que `SKSE_CONFIG` soporta."""
        juego = _crear_juego(tmp_path / "Skyrim", "Skyrim.exe")
        _fijar_registro(monkeypatch, {_REG_LE: str(juego)})

        assert await _detectan_ambos(juego) == (juego, juego)

    async def test_registro_gog(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        juego = _crear_juego(tmp_path / "GOG" / "Skyrim Anniversary Edition", "SkyrimSE.exe")
        _fijar_registro(monkeypatch, {_REG_GOG: str(juego)})

        assert await _detectan_ambos(juego) == (juego, juego)

    async def test_libreria_steam_sse(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        lib = tmp_path / "SteamLibrary"
        juego = _crear_juego(lib / "steamapps" / "common" / "Skyrim Special Edition", "SkyrimSE.exe")
        _fijar_librerias_steam(monkeypatch, [lib])

        assert await _detectan_ambos(juego) == (juego, juego)

    async def test_libreria_steam_legendary_edition(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lib = tmp_path / "SteamLibrary"
        juego = _crear_juego(lib / "steamapps" / "common" / "Skyrim", "Skyrim.exe")
        _fijar_librerias_steam(monkeypatch, [lib])

        assert await _detectan_ambos(juego) == (juego, juego)

    async def test_ruta_comun(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        juego = _crear_juego(tmp_path / "SSE", "SkyrimSE.exe")
        monkeypatch.setattr(scanner_mod, "SKYRIM_COMMON_PATHS", (str(juego),))
        monkeypatch.setattr(auto_mod, "_SKYRIM_COMMON", (str(juego),))

        assert await _detectan_ambos(juego) == (juego, juego)

    async def test_sin_juego_ninguno_inventa_una_ruta(self, tmp_path: pathlib.Path) -> None:
        """Fail-closed: sin fuentes activas los dos devuelven None, no un candidato."""
        assert await _detectan_ambos(tmp_path) == (None, None)
