"""Wiring de producción del ritual de grass en el supervisor (integración §4.3).

El dispatcher ya registra ``analyze_grass_prerequisites``/``generate_grass_cache``
apuntando a ``supervisor._grass_cache_service``; lo que faltaba era darle al
servicio las deps de Fases B/C (perfil MO2, game path, overwrite/Grass). Se
resuelven de forma LAZY (al ejecutar el ritual) porque en la GUI
``MO2_PATH``/``SKYRIM_PATH`` se hidratan después de construir el supervisor
(review Codex #301). Estos tests fijan que ``_build_grass_dependencies`` usa el
validator de modding y el perfil ACTIVO, y que un entorno a medio configurar
devuelve ``None`` (el servicio responde con error de contrato, no crash).
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent
from sky_claw.antigravity.security.path_validator import PathValidator
from sky_claw.local.mo2.grass_profile import GrassProfileManager
from sky_claw.local.mo2.vfs import MO2Controller
from sky_claw.local.tools.grass_cache_service import GrassRuntimeDeps


def _supervisor_con_resolver(
    mo2_root: pathlib.Path | None,
    game_path: pathlib.Path | None,
    validator: PathValidator,
    *,
    profile_name: str = "Default",
) -> SupervisorAgent:
    """Supervisor construction-free con solo lo que _build_grass_dependencies lee."""
    sup = SupervisorAgent.__new__(SupervisorAgent)
    resolver = MagicMock()
    resolver.get_mo2_path.return_value = mo2_root
    resolver.get_skyrim_path.return_value = game_path
    sup._path_resolver = resolver
    sup._modding_validator = validator
    sup.profile_name = profile_name
    return sup


def test_build_grass_dependencies_con_paths_resueltos(tmp_path: pathlib.Path) -> None:
    """Con MO2_PATH y SKYRIM_PATH resueltos, arma las 4 deps reales."""
    mo2_root = tmp_path / "MO2"
    mo2_root.mkdir()
    game = tmp_path / "Skyrim"
    game.mkdir()
    sup = _supervisor_con_resolver(mo2_root, game, PathValidator(roots=[tmp_path]))

    deps = sup._build_grass_dependencies()

    assert isinstance(deps, GrassRuntimeDeps)
    assert isinstance(deps.profile_manager, GrassProfileManager)
    assert isinstance(deps.mo2, MO2Controller)
    assert deps.game_path == game
    assert deps.overwrite_grass_dir == mo2_root / "overwrite" / "Grass"


def test_build_grass_dependencies_clona_el_perfil_activo(tmp_path: pathlib.Path) -> None:
    """Finding #3 Codex: se clona el perfil ACTIVO (self.profile_name), no 'Default'."""
    mo2_root = tmp_path / "MO2"
    mo2_root.mkdir()
    game = tmp_path / "Skyrim"
    game.mkdir()
    sup = _supervisor_con_resolver(mo2_root, game, PathValidator(roots=[tmp_path]), profile_name="MiModlist")

    deps = sup._build_grass_dependencies()

    assert deps is not None
    assert deps.profile_manager._source_profile == "MiModlist"


def test_build_grass_dependencies_usa_el_validator_de_modding(tmp_path: pathlib.Path) -> None:
    """Finding #2 Codex: MO2Controller/GrassProfileManager reciben el validator de
    modding (_modding_validator), no el rollback backup-only."""
    mo2_root = tmp_path / "MO2"
    mo2_root.mkdir()
    game = tmp_path / "Skyrim"
    game.mkdir()
    modding = PathValidator(roots=[tmp_path])
    sup = _supervisor_con_resolver(mo2_root, game, modding)

    deps = sup._build_grass_dependencies()

    assert deps is not None
    assert deps.profile_manager._validator is modding


def test_build_grass_dependencies_sin_mo2_devuelve_none(tmp_path: pathlib.Path) -> None:
    """Sin MO2_PATH la resolución es all-or-nothing: None (no deps parciales)."""
    game = tmp_path / "Skyrim"
    game.mkdir()
    sup = _supervisor_con_resolver(None, game, PathValidator(roots=[tmp_path]))

    assert sup._build_grass_dependencies() is None


def test_build_grass_dependencies_sin_skyrim_devuelve_none(tmp_path: pathlib.Path) -> None:
    """Sin SKYRIM_PATH (Fase C necesita game_path) también None."""
    mo2_root = tmp_path / "MO2"
    mo2_root.mkdir()
    sup = _supervisor_con_resolver(mo2_root, None, PathValidator(roots=[tmp_path]))

    assert sup._build_grass_dependencies() is None
