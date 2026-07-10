"""Tests del resolver de archivos de load order (T-05 de TECHNICAL_REVIEW_TASKS.md).

LOOT corre como subproceso con ``--game-path`` (fuera del VFS de MO2), por lo
que reescribe ``plugins.txt``/``loadorder.txt`` en ``%LOCALAPPDATA%`` — no en
el profile de MO2. El resolver devuelve la UNIÓN de candidatos existentes
(LOCALAPPDATA en sus variantes de tienda, profile MO2 y override explícito)
para que el snapshot de T-06 cubra el archivo que LOOT realmente muta,
cualquiera sea el entorno. Restaurar un archivo no tocado es un no-op, así que
sobre-cubrir es seguro; sub-cubrir es la falsa red de seguridad que el
docstring de ``loot_service`` advierte.
"""

import pathlib

import pytest

from sky_claw.local.mo2.load_order import LoadOrderFileResolver


def _crear_load_order(directorio: pathlib.Path) -> list[pathlib.Path]:
    """Crea plugins.txt y loadorder.txt en *directorio* y devuelve sus rutas."""
    directorio.mkdir(parents=True, exist_ok=True)
    rutas = [directorio / "plugins.txt", directorio / "loadorder.txt"]
    for ruta in rutas:
        ruta.write_text("Skyrim.esm\n", encoding="utf-8")
    return rutas


class TestCandidatosLocalAppData:
    """LOCALAPPDATA es donde LOOT escribe cuando corre fuera del VFS."""

    def test_resuelve_plugins_y_loadorder_de_steam(self, tmp_path: pathlib.Path) -> None:
        esperados = _crear_load_order(tmp_path / "Skyrim Special Edition")

        resolver = LoadOrderFileResolver(local_app_data=tmp_path)
        resultado = resolver.resolve()

        assert sorted(resultado.files) == sorted(esperados)
        assert "localappdata" in resultado.sources

    def test_detecta_variante_gog(self, tmp_path: pathlib.Path) -> None:
        esperados = _crear_load_order(tmp_path / "Skyrim Special Edition GOG")

        resolver = LoadOrderFileResolver(local_app_data=tmp_path)

        assert sorted(resolver.resolve().files) == sorted(esperados)

    def test_detecta_localcache_de_ms_store(self, tmp_path: pathlib.Path) -> None:
        """Game Pass sandboxea el load order bajo Packages\\...\\LocalCache\\Local."""
        local_cache = tmp_path / "Packages" / "BethesdaSoftworks.SkyrimSE-PC_3275kfvn8vcwc" / "LocalCache" / "Local"
        esperados = _crear_load_order(local_cache / "Skyrim Special Edition MS")

        resolver = LoadOrderFileResolver(local_app_data=tmp_path)
        resultado = resolver.resolve()

        assert sorted(resultado.files) == sorted(esperados)
        assert "msstore_localcache" in resultado.sources

    def test_sin_archivos_devuelve_vacio(self, tmp_path: pathlib.Path) -> None:
        resolver = LoadOrderFileResolver(local_app_data=tmp_path)
        resultado = resolver.resolve()

        assert resultado.files == ()
        assert resultado.sources == ()

    def test_sin_localappdata_no_falla(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """En entornos sin LOCALAPPDATA (tests/CI) el resolver no debe crashear."""
        monkeypatch.delenv("LOCALAPPDATA", raising=False)

        resolver = LoadOrderFileResolver()

        assert resolver.resolve().files == ()


class TestAislamientoDelHost:
    """Regresión: en hosts con Skyrim SE instalado, el fallback del resolver a
    ``%LOCALAPPDATA%`` contaminaba todos los tests que no inyectan
    ``local_app_data`` (fallaban 4 tests solo en esas máquinas). El conftest
    aísla la variable por test; este canario lo verifica."""

    def test_resolver_por_defecto_no_ve_el_load_order_del_host(self) -> None:
        resultado = LoadOrderFileResolver().resolve()

        assert resultado.files == ()
        assert resultado.sources == ()


class TestCandidatosMO2:
    """El profile de MO2 cubre el caso LOOT-vía-VFS (y futuros runners)."""

    def test_resuelve_archivos_del_profile(self, tmp_path: pathlib.Path) -> None:
        esperados = _crear_load_order(tmp_path / "profiles" / "Default")

        resolver = LoadOrderFileResolver(mo2_root=tmp_path, profile="Default")
        resultado = resolver.resolve()

        assert sorted(resultado.files) == sorted(esperados)
        assert "mo2_profile" in resultado.sources

    def test_profile_inexistente_devuelve_vacio(self, tmp_path: pathlib.Path) -> None:
        resolver = LoadOrderFileResolver(mo2_root=tmp_path, profile="NoExiste")

        assert resolver.resolve().files == ()

    def test_nombre_de_profile_inseguro_es_rechazado(self, tmp_path: pathlib.Path) -> None:
        """Reutiliza la validación de componentes del repo (path traversal)."""
        with pytest.raises(Exception, match="profile"):
            LoadOrderFileResolver(mo2_root=tmp_path, profile="../evil")


class TestUnionDeCandidatos:
    """Sobre-cubrir es seguro: la unión incluye todos los candidatos existentes."""

    def test_union_localappdata_mas_mo2_sin_duplicados(self, tmp_path: pathlib.Path) -> None:
        de_appdata = _crear_load_order(tmp_path / "appdata" / "Skyrim Special Edition")
        de_mo2 = _crear_load_order(tmp_path / "mo2" / "profiles" / "Default")

        resolver = LoadOrderFileResolver(
            local_app_data=tmp_path / "appdata",
            mo2_root=tmp_path / "mo2",
            profile="Default",
        )
        resultado = resolver.resolve()

        assert sorted(resultado.files) == sorted(de_appdata + de_mo2)
        assert len(set(resultado.files)) == len(resultado.files)

    def test_override_explicito_se_incluye(self, tmp_path: pathlib.Path) -> None:
        esperados = _crear_load_order(tmp_path / "custom")

        resolver = LoadOrderFileResolver(explicit_dir=tmp_path / "custom")
        resultado = resolver.resolve()

        assert sorted(resultado.files) == sorted(esperados)
        assert "override" in resultado.sources
