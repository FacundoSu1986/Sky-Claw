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

import pytest

from sky_claw.antigravity.core.path_resolver import PathResolutionService
from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent
from sky_claw.antigravity.security.path_validator import PathValidator


def _bare_supervisor(backup_validator: PathValidator) -> SupervisorAgent:
    """A SupervisorAgent with only what ``_make_path_resolver`` reads — no heavy
    ``__init__`` (DB, journal, locks, services)."""
    sup = SupervisorAgent.__new__(SupervisorAgent)
    sup.profile_name = "Default"
    sup._path_validator = backup_validator  # the rollback (backup-only) validator
    return sup


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
