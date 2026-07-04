"""Construction smoke test for ``SupervisorAgent.__init__``.

The agent's ``__init__`` wires ~10 services/daemons, yet historically NO test
exercised it — the other supervisor tests build instances via ``__new__``. So
construction bugs (mismatched service kwargs like Blocker 4's
``XEditPipelineService`` missing ``journal``, or the Blocker 3 path validator)
only surfaced at runtime in the packaged GUI exe. This test bootstraps the
real ``__init__`` so that class of bug fails fast in CI instead.
"""

from __future__ import annotations

import pytest

from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent
from sky_claw.antigravity.security.network_gateway import NetworkGateway
from sky_claw.antigravity.security.path_validator import PathValidator


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
