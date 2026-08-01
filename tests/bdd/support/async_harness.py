"""Ownership explícito del event loop usado por escenarios BDD síncronos."""

from __future__ import annotations

import asyncio
from types import TracebackType


class ScenarioLoop:
    """Event loop aislado cuya vida coincide con un escenario BDD."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()

    def __enter__(self) -> ScenarioLoop:
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
            asyncio.set_event_loop(None)
