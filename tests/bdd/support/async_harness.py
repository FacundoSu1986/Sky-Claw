"""Ownership explícito del event loop usado por escenarios BDD síncronos."""

from __future__ import annotations

import asyncio
from types import TracebackType


class ScenarioLoop:
    """Event loop aislado cuya vida coincide con un escenario BDD."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._loop_exterior: asyncio.AbstractEventLoop | None = None
        self._habia_loop_exterior = False

    def __enter__(self) -> ScenarioLoop:
        try:
            self._loop_exterior = asyncio.get_running_loop()
            self._habia_loop_exterior = True
        except RuntimeError:
            # La API pública no permite consultar un loop actual NO running
            # sin crear uno implícitamente. Los policies default de CPython
            # 3.11/3.12 guardan ese estado en ``_local._loop``; leerlo evita
            # fabricar y filtrar un loop al entrar desde un test síncrono.
            policy_local = getattr(asyncio.get_event_loop_policy(), "_local", None)
            self._loop_exterior = getattr(policy_local, "_loop", None)
            self._habia_loop_exterior = self._loop_exterior is not None
        asyncio.set_event_loop(self.loop)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            pendientes = asyncio.all_tasks(self.loop)
            for tarea in pendientes:
                tarea.cancel()
            if pendientes:
                self.loop.run_until_complete(asyncio.gather(*pendientes, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
        finally:
            self.loop.close()
            asyncio.set_event_loop(self._loop_exterior if self._habia_loop_exterior else None)
