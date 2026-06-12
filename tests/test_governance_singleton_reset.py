"""Order-dependent pair proving the autouse GovernanceManager singleton reset.

Without an autouse reset fixture, the sentinel planted by the first test leaks
into the second one (pytest runs tests in file order), breaking test isolation
for every consumer of ``GovernanceManager.get_instance()``.
"""

from __future__ import annotations

from sky_claw.antigravity.security.governance import GovernanceManager


def test_pollute_governance_singleton() -> None:
    """Plant a sentinel instance — the autouse reset must clean it up."""
    GovernanceManager._instance = object()
    assert GovernanceManager._instance is not None


def test_governance_singleton_is_clean_after_previous_test() -> None:
    """Each test must start with a pristine (None) singleton."""
    assert GovernanceManager._instance is None
