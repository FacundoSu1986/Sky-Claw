"""Endurecimiento del ManagedToolExecutor (ítem 7 del arbitraje de auditorías).

Cubre dos grietas de robustez de subprocesos del OODA analysis:

- **E-1 [HIGH]:** ``_stream_telemetry`` drena los pipes con ``readline()`` sin
  timeout y ``execute()`` hacía ``await monitor_task`` sin acotar. Si un
  proceso-nieto hereda el descriptor del pipe (caso real en Windows con xEdit,
  que spawnea hijos), el ``readline()`` nunca ve EOF y el orquestador de modding
  queda colgado indefinidamente. El drenaje ahora está acotado por
  ``drain_timeout``.
- **E-3 [HIGH]:** ``signal_abort`` (invocable desde otro hilo, según su docstring)
  hacía ``if self.proc:`` seguido de ``self.proc.terminate()``. Si un ``abort()``
  concurrente nulea ``self.proc`` entre el check y el uso, el segundo acceso
  lanzaba ``AttributeError`` (no cubierto por ``suppress(ProcessLookupError)``).
  Ahora se captura ``self.proc`` en una referencia local única.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from sky_claw.antigravity.agent.executor import ManagedToolExecutor
from sky_claw.antigravity.security.path_validator import PathValidator


class TestDrenajeAcotadoDeTelemetria:
    """E-1: execute() no cuelga si un stream nunca manda EOF."""

    async def test_execute_no_cuelga_si_un_stream_nunca_manda_eof(self) -> None:
        """Un proceso-nieto que hereda el pipe deja el readline() colgado para
        siempre; execute() debe retornar igual gracias al drenaje acotado."""
        validator = MagicMock(spec=PathValidator)
        executor = ManagedToolExecutor(path_validator=validator, drain_timeout=0.1)

        congelado = asyncio.Event()  # jamás se setea → simula el pipe heredado

        async def _readline_colgado() -> bytes:
            await congelado.wait()
            return b""

        mock_proc = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=_readline_colgado)
        mock_proc.stderr.readline = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)  # el proceso principal YA terminó
        mock_proc.returncode = 0

        with patch(
            "sky_claw.antigravity.agent.executor.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            # Guarda de test: sin el fix, execute() cuelga en ``await monitor_task``
            # y este wait_for lanza TimeoutError → el test falla en rojo.
            result = await asyncio.wait_for(executor.execute("bin", []), timeout=3.0)

        assert result == 0

    async def test_execute_drena_normal_sin_esperar_el_timeout(self) -> None:
        """Camino feliz: con ambos streams en EOF el drenaje completa al instante,
        sin penalizar con la espera de ``drain_timeout``."""
        validator = MagicMock(spec=PathValidator)
        # drain_timeout enorme: si el drenaje esperara el timeout, el test tardaría
        # ~30s; debe completar en milisegundos porque los streams ya están en EOF.
        executor = ManagedToolExecutor(path_validator=validator, drain_timeout=30.0)

        mock_proc = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(return_value=b"")
        mock_proc.stderr.readline = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.returncode = 0

        with patch(
            "sky_claw.antigravity.agent.executor.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await asyncio.wait_for(executor.execute("bin", []), timeout=2.0)

        assert result == 0

    async def test_monitor_no_queda_huerfano_tras_execute(self) -> None:
        """El monitor de telemetría nunca sobrevive a execute() (garantía del
        ``finally``): tras un stream colgado, la tarea queda cancelada/terminada."""
        validator = MagicMock(spec=PathValidator)
        executor = ManagedToolExecutor(path_validator=validator, drain_timeout=0.1)

        congelado = asyncio.Event()

        async def _readline_colgado() -> bytes:
            await congelado.wait()
            return b""

        mock_proc = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=_readline_colgado)
        mock_proc.stderr.readline = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.returncode = 0

        tareas_creadas: list[asyncio.Task[None]] = []
        real_create_task = asyncio.create_task

        def _spy_create_task(coro, **kwargs):  # type: ignore[no-untyped-def]
            task = real_create_task(coro, **kwargs)
            tareas_creadas.append(task)
            return task

        with (
            patch(
                "sky_claw.antigravity.agent.executor.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
            patch(
                "sky_claw.antigravity.agent.executor.asyncio.create_task",
                side_effect=_spy_create_task,
            ),
        ):
            await asyncio.wait_for(executor.execute("bin", []), timeout=3.0)

        # Anclamos TODAS las tareas que execute() crea (no solo "la última"):
        # ninguna debe sobrevivir a execute().
        assert tareas_creadas, "execute() debió crear la tarea de telemetría"
        assert all(t.done() for t in tareas_creadas)


class TestSignalAbortRace:
    """E-3: signal_abort tolera que self.proc se nulee entre el check y el uso."""

    def test_signal_abort_tolera_proc_nulado_entre_check_y_uso(self) -> None:
        """Si un abort() cross-thread nulea self.proc entre el ``if`` y el
        ``terminate()``, signal_abort no debe lanzar AttributeError."""
        executor = ManagedToolExecutor(path_validator=MagicMock())
        proc = MagicMock()
        proc.terminate = MagicMock()

        # PropertyMock reproduce el race: el 1er acceso a ``.proc`` devuelve el
        # proceso; un 2do acceso (como hacía el código viejo) devolvería None
        # → ``None.terminate()`` → AttributeError. El código nuevo captura la
        # referencia una sola vez, así que solo consume el primer valor.
        with patch.object(type(executor), "proc", new_callable=PropertyMock, create=True) as proc_prop:
            proc_prop.side_effect = [proc, None]
            executor.signal_abort()  # no debe lanzar

        proc.terminate.assert_called_once()

    def test_signal_abort_setea_el_evento_aunque_no_haya_proc(self) -> None:
        """Sin proceso vivo, signal_abort igual señaliza el aborto (fail-safe)."""
        executor = ManagedToolExecutor(path_validator=MagicMock())
        executor.proc = None

        executor.signal_abort()

        assert executor._abort_event.is_set()
