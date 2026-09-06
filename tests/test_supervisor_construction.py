"""Construction smoke test for ``SupervisorAgent.__init__``.

The agent's ``__init__`` wires ~10 services/daemons, yet historically NO test
exercised it — the other supervisor tests build instances via ``__new__``. So
construction bugs (mismatched service kwargs like Blocker 4's
``XEditPipelineService`` missing ``journal``, or the Blocker 3 path validator)
only surfaced at runtime in the packaged GUI exe. This test bootstraps the
real ``__init__`` so that class of bug fails fast in CI instead.
"""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.app.orchestrator.dispatcher_dependencies import (
    OrchestrationDispatcherDependencies,
)
from sky_claw.app.orchestrator.supervisor import SupervisorAgent
from sky_claw.app.security.network_gateway import NetworkGateway
from sky_claw.app.security.path_validator import PathValidator


def _ini_portable(base_dir: pathlib.Path) -> str:
    """``ModOrganizer.ini`` portable mínimo apuntando a *base_dir* (formato Qt)."""
    qt = str(base_dir).replace("\\", "/")
    return f"[General]\ngameName=Skyrim Special Edition\n\n[Settings]\nbase_directory={qt}\n"


@pytest.fixture
def mo2_root(tmp_path, monkeypatch):
    """A throwaway MO2 layout + MO2_PATH, with cwd moved into tmp so the
    rollback components' SQLite files land under tmp, not the repo."""
    monkeypatch.chdir(tmp_path)
    mo2 = tmp_path / "MO2"
    (mo2 / "profiles" / "Default").mkdir(parents=True)
    monkeypatch.setenv("MO2_PATH", str(mo2))
    return mo2


def test_supervisor_init_constructs_all_services(mo2_root, tmp_path):
    sandbox = PathValidator(roots=[mo2_root, tmp_path])

    sup = SupervisorAgent(path_validator=sandbox)

    # The construction chain that used to fail only at runtime in the exe:
    assert sup._path_resolver is not None
    assert sup._synthesis_service is not None
    assert sup._dyndolod_service is not None
    assert sup._xedit_service is not None  # Blocker 4: was missing journal kwarg
    assert sup._loot_service is not None
    assert sup._tool_dispatcher is not None
    assert isinstance(sup._dispatcher_dependencies, OrchestrationDispatcherDependencies)
    assert sup._dispatcher_dependencies.scraper is sup.scraper
    assert sup._dispatcher_dependencies.loot_service is sup._loot_service
    assert sup._dispatcher_dependencies.xedit_service is sup._xedit_service
    assert sup._dispatcher_dependencies.dyndolod_service is sup._dyndolod_service
    assert sup._dispatcher_dependencies.pandora_service is sup._pandora_service
    assert sup._dispatcher_dependencies.grass_cache_service is sup._grass_cache_service
    # modlist resolved against the modding sandbox (Blocker 3), not backups:
    assert str(sup.modlist_path).endswith("modlist.txt")
    assert "MO2" in str(sup.modlist_path)


def test_supervisor_injects_provided_gateway(mo2_root, tmp_path):
    """C2: el Supervisor usa el NetworkGateway inyectado (el del AppContext),
    no una instancia propia — así comparte caché DNS + reglas de egress."""
    sandbox = PathValidator(roots=[mo2_root, tmp_path])
    gateway = NetworkGateway()

    sup = SupervisorAgent(path_validator=sandbox, gateway=gateway)

    assert sup.gateway is gateway
    # El scraper egress también debe usar el mismo gateway compartido.
    assert sup.scraper._gateway is gateway


def test_supervisor_creates_own_gateway_by_default(mo2_root, tmp_path):
    """Sin gateway inyectado (tests/standalone), crea el suyo — backward compat."""
    sandbox = PathValidator(roots=[mo2_root, tmp_path])

    sup = SupervisorAgent(path_validator=sandbox)

    assert isinstance(sup.gateway, NetworkGateway)


def test_supervisor_inyecta_runner_vfs_y_guard_f8(mo2_root, tmp_path):
    sandbox = PathValidator(roots=[mo2_root, tmp_path])
    runner = object()

    sup = SupervisorAgent(
        path_validator=sandbox,
        loot_runner=runner,
        require_vfs=True,
    )

    assert sup._loot_service._loot_runner is runner
    assert sup._loot_service._require_vfs is True


def test_supervisor_enhebra_la_instalacion_seleccionada_al_resolver(
    tmp_path,
    monkeypatch,
) -> None:
    """Cable completo AppContext→Supervisor→Resolver (PR #552).

    Ejercita el ``__init__`` real (no ``__new__``): la instalación que el
    composition root selecciona (``mo2_install_dir``) llega al
    ``PathResolutionService`` y GANA sobre ``MO2_PATH``. Modela dos
    instalaciones portables — ``selected`` (la que eligió AppContext) y
    ``wrong_auto`` (la que elegiría ``MO2_PATH``/auto-detección) — cada una con
    su propia instancia de datos. Sin el enhebrado, ``__init__`` resolvía el
    modlist desde ``wrong_auto`` (split-brain instalación-seleccionada !=
    instalación-usada).
    """
    monkeypatch.chdir(tmp_path)
    selected = tmp_path / "selected"
    wrong_auto = tmp_path / "wrong_auto"
    data_selected = tmp_path / "instance_selected"
    data_wrong = tmp_path / "instance_wrong"
    for d in (
        selected,
        wrong_auto,
        data_selected / "mods",
        data_selected / "profiles" / "Default",
        data_wrong / "mods",
        data_wrong / "profiles" / "Default",
    ):
        d.mkdir(parents=True)
    (selected / "ModOrganizer.exe").write_bytes(b"fake exe")
    (wrong_auto / "ModOrganizer.exe").write_bytes(b"fake exe")
    (selected / "ModOrganizer.ini").write_text(_ini_portable(data_selected), encoding="utf-8")
    (wrong_auto / "ModOrganizer.ini").write_text(_ini_portable(data_wrong), encoding="utf-8")
    # MO2_PATH apunta a la instalación EQUIVOCADA a propósito.
    monkeypatch.setenv("MO2_PATH", str(wrong_auto))
    monkeypatch.delenv("MO2_MODS_PATH", raising=False)

    sandbox = PathValidator(roots=[tmp_path])
    sup = SupervisorAgent(path_validator=sandbox, mo2_install_dir=selected)

    # El hint llega al supervisor y al resolver.
    assert sup._mo2_install_dir == selected
    assert sup._path_resolver._mo2_install_dir == selected
    # El resolver resuelve mods desde la instancia de `selected`, NO de MO2_PATH.
    assert sup._path_resolver.get_mo2_mods_path() == (data_selected / "mods").resolve()
    # El modlist que __init__ resolvió cuelga de la MISMA instancia (sin split-brain).
    assert str(data_selected.resolve()) in sup.modlist_path
    assert str(data_wrong.resolve()) not in sup.modlist_path


def test_supervisor_sin_mo2_install_dir_mantiene_legacy(mo2_root, tmp_path) -> None:
    """Sin ``mo2_install_dir`` (default ``None``) el resolver conserva el legacy.

    Backward compat: standalone/tests/callers sin composition root siguen
    decidiendo la instalación vía ``MO2_PATH``/auto-detección.
    """
    sandbox = PathValidator(roots=[mo2_root, tmp_path])

    sup = SupervisorAgent(path_validator=sandbox)

    assert sup._mo2_install_dir is None
    assert sup._path_resolver._mo2_install_dir is None
