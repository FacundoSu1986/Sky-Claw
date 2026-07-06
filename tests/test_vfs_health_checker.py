"""Tests del health-check de VFS (T-13 de TECHNICAL_REVIEW_TASKS.md).

Los symlinks/junctions en las rutas del juego o de MO2 rompen la
virtualización: libloot <0.29 resuelve la ruta real y "sale" del VFS de MO2,
dejando a LOOT ciego ante los mods (informe mmodding §3). A diferencia del
sandboxing de ``path_validator`` (que protege al proceso), este checker
protege al USUARIO: reporta la infraestructura problemática antes de que un
Ritual toque nada.
"""

import pathlib

from sky_claw.local.validators.vfs_health import VfsHealthChecker


def _mo2_con_estructura(base: pathlib.Path) -> pathlib.Path:
    mo2 = base / "MO2"
    for sub in ("mods", "profiles", "overwrite"):
        (mo2 / sub).mkdir(parents=True)
    return mo2


class TestRutasLimpias:
    def test_arbol_sin_symlinks_no_reporta(self, tmp_path: pathlib.Path) -> None:
        game = tmp_path / "Skyrim"
        game.mkdir()
        mo2 = _mo2_con_estructura(tmp_path)

        checker = VfsHealthChecker(game_path=game, mo2_root=mo2)

        assert checker.check() == []

    def test_rutas_inexistentes_no_crashean(self, tmp_path: pathlib.Path) -> None:
        checker = VfsHealthChecker(
            game_path=tmp_path / "no" / "existe",
            mo2_root=tmp_path / "tampoco",
        )

        assert checker.check() == []

    def test_sin_rutas_configuradas_devuelve_vacio(self) -> None:
        assert VfsHealthChecker().check() == []


class TestDeteccion:
    def test_game_path_symlink_es_critico(self, tmp_path: pathlib.Path) -> None:
        """El caso documentado: ruta del juego symlinkeada ciega a LOOT <0.29."""
        real = tmp_path / "SkyrimReal"
        real.mkdir()
        enlace = tmp_path / "Skyrim"
        enlace.symlink_to(real, target_is_directory=True)

        issues = VfsHealthChecker(game_path=enlace).check()

        assert len(issues) == 1
        assert issues[0].path == enlace
        assert issues[0].kind == "symlink"
        assert issues[0].severity == "critical"
        assert "LOOT" in issues[0].remediation
        assert "0.29" in issues[0].remediation

    def test_ancestro_symlink_tambien_se_detecta(self, tmp_path: pathlib.Path) -> None:
        """El symlink puede estar arriba en el árbol (ej: D:\\Games enlazado)."""
        real = tmp_path / "discos" / "d"
        real.mkdir(parents=True)
        enlace = tmp_path / "Games"
        enlace.symlink_to(real, target_is_directory=True)
        game = enlace / "Skyrim"
        game.mkdir()

        issues = VfsHealthChecker(game_path=game).check()

        assert any(i.path == enlace and i.severity == "critical" for i in issues)

    def test_mod_symlinkeado_en_mods_es_warning(self, tmp_path: pathlib.Path) -> None:
        mo2 = _mo2_con_estructura(tmp_path)
        real = tmp_path / "ModRealFueraDeMO2"
        real.mkdir()
        (mo2 / "mods" / "MiModEnlazado").symlink_to(real, target_is_directory=True)

        issues = VfsHealthChecker(mo2_root=mo2).check()

        assert len(issues) == 1
        assert issues[0].kind == "symlink"
        assert issues[0].severity == "warning"

    def test_overwrite_symlinkeado_se_detecta(self, tmp_path: pathlib.Path) -> None:
        mo2 = _mo2_con_estructura(tmp_path)
        real = tmp_path / "OverwriteReal"
        (mo2 / "overwrite").rmdir()
        real.mkdir()
        (mo2 / "overwrite").symlink_to(real, target_is_directory=True)

        issues = VfsHealthChecker(mo2_root=mo2).check()

        assert any(i.path == mo2 / "overwrite" for i in issues)

    def test_sin_duplicados_cuando_game_y_mo2_comparten_ancestro(self, tmp_path: pathlib.Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        enlace = tmp_path / "Base"
        enlace.symlink_to(real, target_is_directory=True)
        game = enlace / "Skyrim"
        game.mkdir()
        mo2 = _mo2_con_estructura(enlace)

        issues = VfsHealthChecker(game_path=game, mo2_root=mo2).check()

        assert len([i for i in issues if i.path == enlace]) == 1
