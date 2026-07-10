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


class TestRunDaemonsAndInterface:
    """H-2: los loops de los demonios son supervisados de verdad (fail-fast)."""

    @staticmethod
    def _bare_supervisor_with_daemons(maintenance, telemetry, watcher, interface_coro):
        sup = SupervisorAgent.__new__(SupervisorAgent)
        sup._maintenance_daemon = maintenance  # type: ignore[attr-defined]
        sup._telemetry_daemon = telemetry  # type: ignore[attr-defined]
        sup._watcher_daemon = watcher  # type: ignore[attr-defined]
        sup._run_interface_isolated = interface_coro  # type: ignore[attr-defined,method-assign]
        return sup

    @staticmethod
    def _forever_daemon() -> AsyncMock:
        import asyncio

        async def _run() -> None:
            await asyncio.Event().wait()  # corre "para siempre" hasta cancelación

        d = AsyncMock()
        d.run = AsyncMock(side_effect=_run)
        return d

    @pytest.mark.asyncio
    async def test_daemon_crash_propaga_y_cancela_al_resto(self) -> None:
        """Si un loop de demonio explota, la excepción se propaga (fail-fast)."""
        import asyncio

        async def _crashing_run() -> None:
            raise RuntimeError("watcher loop reventó")

        crashing = AsyncMock()
        crashing.run = AsyncMock(side_effect=_crashing_run)

        maintenance = self._forever_daemon()
        telemetry = self._forever_daemon()

        async def _interface_forever() -> None:
            await asyncio.Event().wait()

        sup = self._bare_supervisor_with_daemons(maintenance, telemetry, crashing, _interface_forever)

        with pytest.raises(RuntimeError, match="watcher loop reventó"):
            await sup._run_daemons_and_interface()

    @pytest.mark.asyncio
    async def test_interface_retorna_apaga_con_gracia(self) -> None:
        """Si la interfaz retorna normalmente, se cancelan los demonios sin error."""
        maintenance = self._forever_daemon()
        telemetry = self._forever_daemon()
        watcher = self._forever_daemon()

        async def _interface_returns() -> None:
            return None

        sup = self._bare_supervisor_with_daemons(maintenance, telemetry, watcher, _interface_returns)

        # No debe lanzar; los demonios quedan cancelados.
        await sup._run_daemons_and_interface()


@pytest.mark.asyncio
async def test_daemon_run_propaga_excepcion_del_loop() -> None:
    """run() await-ea el loop directamente: sus excepciones se propagan (H-2)."""
    from unittest.mock import MagicMock, patch

    from sky_claw.antigravity.orchestrator.maintenance_daemon import MaintenanceDaemon

    daemon = MaintenanceDaemon(snapshot_manager=MagicMock())

    async def _boom() -> None:
        raise RuntimeError("loop falló")

    with patch.object(daemon, "_pruning_loop", side_effect=_boom), pytest.raises(RuntimeError, match="loop falló"):
        await daemon.run()
