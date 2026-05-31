"""P1.2 — CoreEventBus must support a fail-loud DLQ requirement.

In production a missing DLQ is a configuration bug, not a runtime
fallback. The original ``CoreEventBus`` silently dropped events under
backpressure when ``dlq=None`` (``# H-04 ... preservar drop silencioso``),
which hides operator misconfiguration behind a WARNING line and a vanished
event. P1.2 adds a ``require_dlq`` flag so production deployments fail
fast at construction when the DLQ is missing, while keeping the legacy
behavior (dlq optional, silent drop) for tests and dev shells.

Contracts:
- ``require_dlq=False`` (default): backward compatible — None DLQ works.
- ``require_dlq=True, dlq=None``: raises ``ValueError`` at construction.
- ``require_dlq=True, dlq=<dlq>``: works exactly like the legacy DLQ path.
- ``create_bus_with_dlq()`` factory now passes ``require_dlq=True`` so
  production starts always have a DLQ wired up.
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

import pytest

from sky_claw.antigravity.core.event_bus import CoreEventBus, create_bus_with_dlq


class TestRequireDlqConstruction:
    def test_default_construction_is_backward_compatible(self) -> None:
        """``CoreEventBus()`` with no args must keep working (None DLQ allowed)."""
        bus = CoreEventBus()
        assert bus._dlq is None

    def test_explicit_dlq_works_unchanged(self) -> None:
        """Passing a DLQ without require_dlq mirrors the original API."""
        fake_dlq = MagicMock()
        bus = CoreEventBus(dlq=fake_dlq)
        assert bus._dlq is fake_dlq

    def test_require_dlq_without_dlq_raises_at_init(self) -> None:
        """Production safety: configuration bug surfaces immediately."""
        with pytest.raises(ValueError, match="require_dlq"):
            CoreEventBus(require_dlq=True)

    def test_require_dlq_with_none_dlq_raises_at_init(self) -> None:
        """Explicit dlq=None with require_dlq=True is still a config error."""
        with pytest.raises(ValueError, match="require_dlq"):
            CoreEventBus(require_dlq=True, dlq=None)

    def test_require_dlq_with_valid_dlq_constructs_fine(self) -> None:
        """When the DLQ is wired up, require_dlq=True is a no-op."""
        fake_dlq = MagicMock()
        bus = CoreEventBus(require_dlq=True, dlq=fake_dlq)
        assert bus._dlq is fake_dlq


class TestFactoryEnforcesDlq:
    def test_create_bus_with_dlq_produces_a_bus_that_has_dlq(self, tmp_path: pathlib.Path) -> None:
        """The production factory must always emit a bus with a real DLQ."""
        bus = create_bus_with_dlq(db_path=tmp_path / "dlq.db")
        assert bus._dlq is not None, (
            "create_bus_with_dlq must always wire up a DLQ — production deploys depend on this invariant"
        )
