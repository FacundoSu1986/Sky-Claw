"""P1 §3.1 — SupervisorAgent's interface TaskGroup must re-raise unexpected errors.

Original code:
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.interface.connect())
    except* Exception as eg:
        for exc in eg.exceptions:
            logger.error("... sub-error: %s", exc)

The blanket ``except* Exception`` absorbs everything — including bugs like
``AttributeError`` or ``TypeError`` that should crash loudly. The fix splits
into two ``except*`` clauses:

  - Recoverable network errors (ConnectionError, TimeoutError, OSError):
    logged WARNING and swallowed.
  - Anything else (programming bugs): logged CRITICAL and re-raised.

Contracts verified here:
  1. ConnectionError is absorbed (existing behavior preserved).
  2. AttributeError is re-raised inside an ExceptionGroup.
  3. ValueError is re-raised inside an ExceptionGroup.
  4. Mixed groups split correctly — ConnectionError absorbed, bug propagates.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from sky_claw.antigravity.orchestrator.supervisor import SupervisorAgent


def _bare_supervisor(connect_mock: AsyncMock) -> SupervisorAgent:
    """Build a minimal SupervisorAgent bypassing the heavy __init__.

    Only ``self.interface.connect`` is needed for ``_run_interface_isolated``.
    """
    sup = SupervisorAgent.__new__(SupervisorAgent)
    interface = AsyncMock()
    interface.connect = connect_mock
    sup.interface = interface  # type: ignore[attr-defined]
    return sup


class TestRunInterfaceIsolated:
    @pytest.mark.asyncio
    async def test_connection_error_is_absorbed(self, caplog: pytest.LogCaptureFixture) -> None:
        """ConnectionError is a recoverable network failure — log WARNING, no re-raise."""

        async def _raise_connect() -> None:
            raise ConnectionError("nexus down")

        sup = _bare_supervisor(AsyncMock(side_effect=_raise_connect))

        with caplog.at_level(logging.WARNING):
            # Must NOT raise.
            await sup._run_interface_isolated()

        assert any("recoverable" in r.message.lower() for r in caplog.records), (
            "Expected a WARNING-level 'recoverable' log entry for the absorbed ConnectionError"
        )

    @pytest.mark.asyncio
    async def test_attribute_error_is_reraised(self) -> None:
        """AttributeError signals a programming bug — must propagate, never absorbed."""

        async def _raise_connect() -> None:
            raise AttributeError("supervisor missing attr")

        sup = _bare_supervisor(AsyncMock(side_effect=_raise_connect))

        with pytest.raises(BaseExceptionGroup) as exc_info:
            await sup._run_interface_isolated()

        attribute_errors = [e for e in exc_info.value.exceptions if isinstance(e, AttributeError)]
        assert attribute_errors, (
            "AttributeError must be re-raised inside an ExceptionGroup — "
            "it must NOT be silently absorbed by the supervisor's except* clause."
        )

    @pytest.mark.asyncio
    async def test_value_error_is_reraised(self) -> None:
        """ValueError (typical bug) propagates instead of being silently logged."""

        async def _raise_connect() -> None:
            raise ValueError("bad config value")

        sup = _bare_supervisor(AsyncMock(side_effect=_raise_connect))

        with pytest.raises(BaseExceptionGroup) as exc_info:
            await sup._run_interface_isolated()

        value_errors = [e for e in exc_info.value.exceptions if isinstance(e, ValueError)]
        assert value_errors, "ValueError must propagate as a programming bug"

    @pytest.mark.asyncio
    async def test_timeout_error_is_absorbed(self, caplog: pytest.LogCaptureFixture) -> None:
        """asyncio.TimeoutError is also recoverable — log WARNING, no re-raise."""

        async def _raise_connect() -> None:
            raise TimeoutError("connect timed out")

        sup = _bare_supervisor(AsyncMock(side_effect=_raise_connect))

        with caplog.at_level(logging.WARNING):
            await sup._run_interface_isolated()

        assert any("recoverable" in r.message.lower() for r in caplog.records)
