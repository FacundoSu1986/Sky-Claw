"""Wiring de producción del ritual de grass en el supervisor (integración §4.3).

El dispatcher ya registra ``analyze_grass_prerequisites``/``generate_grass_cache``
apuntando a ``supervisor._grass_cache_service``; lo que faltaba era darle al
servicio las deps de Fases B/C (perfil MO2, game path, overwrite/Grass). Estos
tests fijan que ``_build_grass_dependencies`` las arma desde el path resolver y
que un entorno a medio configurar NO rompe el arranque (deps ``None`` → el
servicio devuelve error de contrato, no crash).
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent
from sky_claw.antigravity.security.path_validator import PathValidator
from sky_claw.local.mo2.grass_profile import GrassProfileManager
from sky_claw.local.mo2.vfs import MO2Controller


def _supervisor_con_resolver(
    mo2_root: pathlib.Path | None, game_path: pathlib.Path | None, validator: PathValidator
) -> SupervisorAgent:
    """Supervisor construction-free con solo lo que _build_grass_dependencies lee."""
    sup = SupervisorAgent.__new__(SupervisorAgent)
    resolver = MagicMock()
    resolver.get_mo2_path.return_value = mo2_root
    resolver.get_skyrim_path.return_value = game_path
    sup._path_resolver = resolver
    sup._path_validator = validator
    return sup


def test_build_grass_dependencies_con_paths_resueltos(tmp_path: pathlib.Path) -> None:
    """Con MO2_PATH y SKYRIM_PATH resueltos, arma las 4 deps reales."""
    mo2_root = tmp_path / "MO2"
    mo2_root.mkdir()
    game = tmp_path / "Skyrim"
    game.mkdir()
    sup = _supervisor_con_resolver(mo2_root, game, PathValidator(roots=[tmp_path]))

    pm, mo2, game_path, overwrite = sup._build_grass_dependencies()

    assert isinstance(pm, GrassProfileManager)
    assert isinstance(mo2, MO2Controller)
    assert game_path == game
    assert overwrite == mo2_root / "overwrite" / "Grass"


def test_build_grass_dependencies_sin_mo2_devuelve_none(tmp_path: pathlib.Path) -> None:
    """Sin MO2_PATH: perfil/mo2/overwrite quedan None, pero game_path igual se resuelve."""
    game = tmp_path / "Skyrim"
    game.mkdir()
    sup = _supervisor_con_resolver(None, game, PathValidator(roots=[tmp_path]))

    pm, mo2, game_path, overwrite = sup._build_grass_dependencies()

    assert pm is None
    assert mo2 is None
    assert overwrite is None
    assert game_path == game


def test_build_grass_dependencies_entorno_vacio_no_rompe(tmp_path: pathlib.Path) -> None:
    """Entorno a medio configurar (ni MO2 ni Skyrim): las 4 son None, sin excepción."""
    sup = _supervisor_con_resolver(None, None, PathValidator(roots=[tmp_path]))

    assert sup._build_grass_dependencies() == (None, None, None, None)
