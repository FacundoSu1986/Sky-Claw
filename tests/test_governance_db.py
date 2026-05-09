"""Tests M-01 PR C: GovernanceManager con DatabaseLifecycleManager.

Verifica que:
- ``set_lifecycle()`` + happy path de update_scan_result/is_scanned_and_clean
  usan la conexión del DatabaseLifecycleManager.
- Sin lifecycle inyectado, ``is_scanned_and_clean`` falla-cerrado (False).
- ``update_scan_result`` registra la cache_db_path en ``lifecycle.managed_paths``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from sky_claw.antigravity.core.db_lifecycle import (
    DatabaseLifecycleConfig,
    DatabaseLifecycleManager,
)
from sky_claw.antigravity.security.governance import GovernanceManager


@pytest.fixture
def lifecycle() -> DatabaseLifecycleManager:
    """DatabaseLifecycleManager vacío con signal handlers desactivados (test-safe)."""
    return DatabaseLifecycleManager(
        db_paths=[],
        config=DatabaseLifecycleConfig(enable_signal_handlers=False),
    )


@pytest.fixture
def governance(
    tmp_path: Path, lifecycle: DatabaseLifecycleManager
) -> Iterator[GovernanceManager]:
    """GovernanceManager con lifecycle inyectado y singleton aislado por test."""
    GovernanceManager._instance = None
    gov = GovernanceManager.get_instance(base_path=str(tmp_path))
    gov.set_lifecycle(lifecycle)
    yield gov
    GovernanceManager._instance = None


@pytest.mark.asyncio
async def test_update_then_is_scanned_returns_true(
    governance: GovernanceManager,
    lifecycle: DatabaseLifecycleManager,
    tmp_path: Path,
) -> None:
    """Happy path: update_scan_result(CLEAN) + is_scanned_and_clean -> True."""
    f = tmp_path / "sample.py"
    f.write_text("print('hi')")

    await governance.update_scan_result(str(f), results=[], status="CLEAN")
    assert await governance.is_scanned_and_clean(str(f)) is True

    await lifecycle.shutdown_all()


@pytest.mark.asyncio
async def test_is_scanned_returns_false_without_lifecycle(tmp_path: Path) -> None:
    """Fail-closed: sin lifecycle, is_scanned_and_clean retorna False sin crash."""
    GovernanceManager._instance = None
    try:
        gov = GovernanceManager.get_instance(base_path=str(tmp_path))
        # Notar: NO se llama set_lifecycle()

        f = tmp_path / "sample.py"
        f.write_text("print('hi')")
        assert await gov.is_scanned_and_clean(str(f)) is False
    finally:
        GovernanceManager._instance = None


@pytest.mark.asyncio
async def test_governance_uses_lifecycle_connection(
    governance: GovernanceManager,
    lifecycle: DatabaseLifecycleManager,
    tmp_path: Path,
) -> None:
    """update_scan_result registra la cache_db_path en lifecycle.managed_paths."""
    f = tmp_path / "sample.py"
    f.write_text("hi")

    await governance.update_scan_result(str(f), results=[], status="CLEAN")
    assert str(governance.cache_db_path) in lifecycle.managed_paths

    await lifecycle.shutdown_all()
