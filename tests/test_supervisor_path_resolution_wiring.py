"""Regression: the GUI ``SupervisorAgent`` must resolve MO2 paths against the
*modding* sandbox validator, not the backup-only rollback validator.

Blocker 3 (broke the packaging-fixed exe at runtime): ``SupervisorAgent`` fed
its rollback ``PathValidator`` (roots = ``[.skyclaw_backups]``) to the
``PathResolutionService``, so every real MO2 path was rejected and the agent
never bootstrapped — ``RuntimeError: No se pudo resolver ... modlist``.

``SupervisorAgent.__init__`` is heavy and has no test coverage (the other
supervisor tests build it via ``__new__``), which is exactly why this stayed
green in CI while the exe failed. These tests cover the wiring seam directly.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from sky_claw.app.core.path_resolver import PathResolutionService
from sky_claw.app.orchestrator.supervisor import SupervisorAgent
from sky_claw.app.security.path_validator import PathValidator


def _bare_supervisor(backup_validator: PathValidator) -> SupervisorAgent:
    """A SupervisorAgent with only what ``_make_path_resolver`` reads — no heavy
    ``__init__`` (DB, journal, locks, services)."""
    sup = SupervisorAgent.__new__(SupervisorAgent)
    sup.profile_name = "Default"
    sup._path_validator = backup_validator  # the rollback (backup-only) validator
    return sup


def _sky_claw_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1] / "sky_claw"


def test_make_path_resolver_prefers_injected_sandbox_validator(tmp_path) -> None:
    backup_only = PathValidator(roots=[tmp_path / ".skyclaw_backups"])
    sandbox = PathValidator(roots=[tmp_path / "MO2"])
    sup = _bare_supervisor(backup_only)

    resolver = sup._make_path_resolver(sandbox)

    assert isinstance(resolver, PathResolutionService)
    # MO2 resolution must use the modding sandbox, NOT the backup validator.
    assert resolver._path_validator is sandbox


def test_make_path_resolver_falls_back_to_rollback_validator_when_none(tmp_path) -> None:
    backup_only = PathValidator(roots=[tmp_path / ".skyclaw_backups"])
    sup = _bare_supervisor(backup_only)

    resolver = sup._make_path_resolver(None)

    assert resolver._path_validator is backup_only


def test_sandbox_validator_resolves_mo2_modlist(tmp_path, monkeypatch) -> None:
    """With a validator whose roots include the MO2 root, the modlist resolves
    (it raised under the backup-only validator — Blocker 3)."""
    mo2 = tmp_path / "MO2"
    (mo2 / "profiles" / "Default").mkdir(parents=True)
    monkeypatch.setenv("MO2_PATH", str(mo2))

    resolver = PathResolutionService(path_validator=PathValidator(roots=[mo2]), profile_name="Default")

    assert resolver.resolve_modlist_path("Default") == mo2 / "profiles" / "Default" / "modlist.txt"


def test_backup_only_validator_rejects_mo2_modlist(tmp_path, monkeypatch) -> None:
    """Characterizes Blocker 3: the backup-only validator rejects every MO2
    path, so it must never be the one used for resolution."""
    mo2 = tmp_path / "MO2"
    (mo2 / "profiles" / "Default").mkdir(parents=True)
    monkeypatch.setenv("MO2_PATH", str(mo2))

    resolver = PathResolutionService(
        path_validator=PathValidator(roots=[tmp_path / ".skyclaw_backups"]),
        profile_name="Default",
    )

    with pytest.raises(RuntimeError):
        resolver.resolve_modlist_path("Default")


# ---------------------------------------------------------------------------
# PR #552: la instalación MO2 seleccionada por AppContext debe llegar al
# resolver (cable AppContext → SupervisorAgent → PathResolutionService).
# ---------------------------------------------------------------------------


def test_make_path_resolver_enhebra_mo2_install_dir(tmp_path) -> None:
    """``_make_path_resolver`` pasa el ``mo2_install_dir`` recibido al resolver.

    Es la costura que ``SupervisorAgent.__init__`` usa para reutilizar la
    instalación que AppContext seleccionó, en vez de re-decidir vía
    ``MO2_PATH``/auto-detección.
    """
    sandbox = PathValidator(roots=[tmp_path])
    selected = tmp_path / "selected"
    sup = _bare_supervisor(PathValidator(roots=[tmp_path / ".skyclaw_backups"]))

    resolver = sup._make_path_resolver(sandbox, mo2_install_dir=selected)

    assert resolver._mo2_install_dir == selected


def test_make_path_resolver_sin_install_dir_es_none(tmp_path) -> None:
    """Backward compat: sin ``mo2_install_dir`` el resolver queda en legacy."""
    sandbox = PathValidator(roots=[tmp_path])
    sup = _bare_supervisor(PathValidator(roots=[tmp_path / ".skyclaw_backups"]))

    resolver = sup._make_path_resolver(sandbox)

    assert resolver._mo2_install_dir is None


def test_cable_appcontext_publica_y_bootloader_pasa_mo2_install_dir() -> None:
    """Ancla del cable AppContext → Supervisor (los dos eslabones no booteables).

    El ``__init__`` del ``SupervisorAgent`` productivo lo arma el bootloader de
    la GUI (``start_full`` corre antes y no lo instancia), así que este cable no
    se puede ejercitar arrancando la app en un test. Se congela por AST:

    1. ``app_context.py`` publica ``self.mo2_install_dir = mo2_root``.
    2. ``_bootloader.py`` construye ``SupervisorAgent(...,
       mo2_install_dir=ctx.mo2_install_dir)``.

    Romper cualquiera de los dos deja al resolver del supervisor re-decidiendo
    la instalación por su cuenta (el bug del PR #552).
    """
    raiz = _sky_claw_root()

    # 1. AppContext publica mo2_root como self.mo2_install_dir.
    ac = ast.parse((raiz / "app_context.py").read_text(encoding="utf-8"))
    publica = any(
        isinstance(nodo, ast.Assign)
        and any(
            isinstance(t, ast.Attribute)
            and t.attr == "mo2_install_dir"
            and isinstance(t.value, ast.Name)
            and t.value.id == "self"
            for t in nodo.targets
        )
        and isinstance(nodo.value, ast.Name)
        and nodo.value.id == "mo2_root"
        for nodo in ast.walk(ac)
    )
    assert publica, "AppContext debe publicar `self.mo2_install_dir = mo2_root` en start_full."

    # 2. El bootloader pasa ctx.mo2_install_dir al SupervisorAgent.
    bl = ast.parse((raiz / "app" / "gui" / "_bootloader.py").read_text(encoding="utf-8"))
    pasa = False
    for nodo in ast.walk(bl):
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name) and nodo.func.id == "SupervisorAgent":
            for kw in nodo.keywords:
                if (
                    kw.arg == "mo2_install_dir"
                    and isinstance(kw.value, ast.Attribute)
                    and kw.value.attr == "mo2_install_dir"
                    and isinstance(kw.value.value, ast.Name)
                    and kw.value.value.id == "ctx"
                ):
                    pasa = True
    assert pasa, (
        "El bootloader de la GUI debe construir SupervisorAgent con "
        "mo2_install_dir=ctx.mo2_install_dir (cable AppContext→Supervisor)."
    )
