"""Tests de los builders compartidos de sensores de preflight (T-16d).

Estos builders extraen la costura que ``loot_service``, ``xedit_service`` y
``synthesis_service`` duplicaban en sus ``_ensure_preflight`` (construcción del
``VfsHealthChecker`` con guard de rutas + closures de masters/límites y
overwrite). Los tests fijan el contrato compartido: coacción de rutas no-``Path``
a ``None``, gate de honestidad ante fuentes vacías y **freshness** (los closures
re-resuelven en cada llamada, review Codex #252).
"""

from __future__ import annotations

from sky_claw.local.mo2.plugin_sources import PluginSources
from sky_claw.local.validators.missing_masters import MasterIssue
from sky_claw.local.validators.overwrite_health import OverwriteScan
from sky_claw.local.validators.plugin_limits import LoadOrderLimits
from sky_claw.local.validators.preflight_sensors import (
    build_mo2_profile_sources_resolver,
    build_modlist_sensors,
    build_overwrite_sensor,
    build_vfs_sensor,
)
from sky_claw.local.validators.vfs_health import VfsHealthChecker


class TestBuildVfsSensor:
    def test_sin_ninguna_raiz_devuelve_none(self):
        assert build_vfs_sensor(raw_game=None, raw_mo2=None, scan_mods_dir=False) is None

    def test_con_solo_game_construye_el_checker(self, tmp_path):
        checker = build_vfs_sensor(raw_game=tmp_path, raw_mo2=None, scan_mods_dir=True)
        assert isinstance(checker, VfsHealthChecker)
        assert checker._game_path == tmp_path
        assert checker._mo2_root is None
        assert checker._scan_mods_dir is True

    def test_con_solo_mo2_construye_el_checker(self, tmp_path):
        checker = build_vfs_sensor(raw_game=None, raw_mo2=tmp_path, scan_mods_dir=False)
        assert isinstance(checker, VfsHealthChecker)
        assert checker._mo2_root == tmp_path
        assert checker._game_path is None
        assert checker._scan_mods_dir is False

    def test_coacciona_no_path_a_none(self):
        # Un path_resolver mockeado puede devolver algo que no es Path.
        assert build_vfs_sensor(raw_game="no-soy-path", raw_mo2=object(), scan_mods_dir=False) is None

    def test_coacciona_solo_el_no_path(self, tmp_path):
        # game es no-Path pero mo2 sí → el checker se construye solo con mo2.
        checker = build_vfs_sensor(raw_game="x", raw_mo2=tmp_path, scan_mods_dir=True)
        assert isinstance(checker, VfsHealthChecker)
        assert checker._game_path is None
        assert checker._mo2_root == tmp_path


class TestBuildModlistSensors:
    def _sources_reales(self, tmp_path) -> PluginSources:
        mod_dir = tmp_path / "mods" / "ModA"
        mod_dir.mkdir(parents=True)
        (mod_dir / "A.esp").write_bytes(b"")
        return PluginSources(plugin_dirs=(mod_dir,), enabled_plugins=("A.esp",))

    def test_fuentes_vacias_devuelve_none_none(self):
        masters, limits = build_modlist_sensors(lambda: PluginSources(plugin_dirs=(), enabled_plugins=()))
        assert masters is None
        assert limits is None

    def test_sin_plugins_habilitados_devuelve_none_none(self, tmp_path):
        sources = PluginSources(plugin_dirs=(tmp_path,), enabled_plugins=())
        masters, limits = build_modlist_sensors(lambda: sources)
        assert masters is None
        assert limits is None

    def test_con_fuentes_construye_ambos_closures(self, tmp_path):
        sources = self._sources_reales(tmp_path)
        masters, limits = build_modlist_sensors(lambda: sources)
        assert masters is not None
        assert limits is not None
        resultado_masters = masters()
        assert isinstance(resultado_masters, list)
        assert all(isinstance(issue, MasterIssue) for issue in resultado_masters)
        assert isinstance(limits(), LoadOrderLimits)

    def test_freshness_reresuelve_por_llamada(self, tmp_path):
        sources = self._sources_reales(tmp_path)
        llamadas = {"n": 0}

        def resolver() -> PluginSources:
            llamadas["n"] += 1
            return sources

        masters, limits = build_modlist_sensors(resolver)
        base = llamadas["n"]  # el gate de honestidad ya resolvió una vez al construir
        assert masters is not None
        assert limits is not None
        masters()
        limits()
        assert llamadas["n"] == base + 2  # cada closure re-resuelve independientemente


class TestBuildOverwriteSensor:
    def test_none_devuelve_none(self):
        assert build_overwrite_sensor(None) is None

    def test_no_path_devuelve_none(self):
        assert build_overwrite_sensor("x") is None  # type: ignore[arg-type]

    def test_con_dir_construye_closure(self, tmp_path):
        overwrite = tmp_path / "overwrite"
        overwrite.mkdir()
        (overwrite / "residuo.txt").write_text("x")
        sensor = build_overwrite_sensor(overwrite)
        assert sensor is not None
        scan = sensor()
        assert isinstance(scan, OverwriteScan)
        assert "residuo.txt" in scan.files

    def test_freshness_reescanea_por_llamada(self, tmp_path):
        overwrite = tmp_path / "overwrite"
        overwrite.mkdir()
        sensor = build_overwrite_sensor(overwrite)
        assert sensor is not None
        assert sensor().files == ()
        (overwrite / "nuevo.txt").write_text("x")
        # El closure re-escanea: ve el archivo aparecido entre corridas (freshness).
        assert "nuevo.txt" in sensor().files


class TestBuildMo2ProfileSourcesResolver:
    """Resolver de fuentes desde el perfil MO2 activo (T-16c·2/3): lee
    ``profiles/<perfil>/plugins.txt``, NO el %LOCALAPPDATA% global."""

    def _fixture(self, tmp_path):
        game = tmp_path / "Skyrim"
        (game / "Data").mkdir(parents=True)
        mo2 = tmp_path / "MO2"
        (mo2 / "mods" / "ModA").mkdir(parents=True)
        (mo2 / "mods" / "ModA" / "A.esp").write_bytes(b"TES4")
        (mo2 / "overwrite").mkdir()
        profile_dir = mo2 / "profiles" / "Default"
        profile_dir.mkdir(parents=True)
        (profile_dir / "plugins.txt").write_bytes(b"\xef\xbb\xbf*A.esp\r\n")
        return game, mo2

    def test_perfil_resoluble_devuelve_closure_de_fuentes(self, tmp_path):
        game, mo2 = self._fixture(tmp_path)
        resolver = build_mo2_profile_sources_resolver(game=game, mo2=mo2, profile="Default")
        assert resolver is not None
        sources = resolver()
        assert isinstance(sources, PluginSources)
        assert "A.esp" in sources.enabled_plugins  # solo activos con `*`

    def test_profile_no_str_devuelve_none(self, tmp_path):
        game, mo2 = self._fixture(tmp_path)
        assert build_mo2_profile_sources_resolver(game=game, mo2=mo2, profile=None) is None

    def test_sin_plugins_txt_en_el_perfil_devuelve_none(self, tmp_path):
        game = tmp_path / "Skyrim"
        (game / "Data").mkdir(parents=True)
        mo2 = tmp_path / "MO2"
        (mo2 / "profiles" / "Default").mkdir(parents=True)  # perfil SIN plugins.txt
        assert build_mo2_profile_sources_resolver(game=game, mo2=mo2, profile="Default") is None

    def test_perfil_inseguro_devuelve_none(self, tmp_path):
        game, mo2 = self._fixture(tmp_path)
        # Un nombre de perfil con traversal no debe resolver (guard assert_safe_component).
        assert build_mo2_profile_sources_resolver(game=game, mo2=mo2, profile="../evil") is None
