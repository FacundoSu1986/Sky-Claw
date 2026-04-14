"""Tests para WatcherDaemon migrado al CoreEventBus.

Verifica que el demonio publica eventos estructurados con
topic ``system.modlist.changed`` en lugar de invocar un callback.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from sky_claw.core.event_bus import CoreEventBus, Event
from sky_claw.core.event_payloads import ModlistChangedPayload
from sky_claw.orchestrator.watcher_daemon import WatcherDaemon

if TYPE_CHECKING:
    import pathlib


@pytest.fixture
def mock_db() -> AsyncMock:
    """DatabaseAgent mock con get_memory/set_memory asíncronos."""
    db = AsyncMock()
    db.get_memory.return_value = "0.0"
    return db


@pytest.fixture
def event_bus() -> CoreEventBus:
    """CoreEventBus fresco para cada test."""
    return CoreEventBus()


@pytest.fixture
def watcher(
    mock_db: AsyncMock, event_bus: CoreEventBus, tmp_path: pathlib.Path
) -> WatcherDaemon:
    """WatcherDaemon configurado con EventBus y archivo temporal."""
    modlist_file = tmp_path / "modlist.txt"
    modlist_file.write_text("+plugin1.esp\n+plugin2.esm\n")
    return WatcherDaemon(
        modlist_path=str(modlist_file),
        profile_name="TestProfile",
        db=mock_db,
        event_bus=event_bus,
        interval=0.1,
    )


class TestModlistChangedPayload:
    """Tests para el payload Pydantic del evento."""

    def test_payload_creation(self) -> None:
        """Payload se crea correctamente con todos los campos."""
        payload = ModlistChangedPayload(
            profile_name="Default",
            modlist_path="/mnt/c/Modding/MO2/profiles/Default/modlist.txt",
            previous_mtime=1000.0,
            current_mtime=2000.0,
        )
        assert payload.profile_name == "Default"
        assert payload.previous_mtime == 1000.0
        assert payload.current_mtime == 2000.0
        assert payload.detected_at > 0.0

    def test_payload_frozen(self) -> None:
        """Payload es inmutable (frozen=True)."""
        payload = ModlistChangedPayload(
            profile_name="Default",
            modlist_path="/path/to/modlist.txt",
            previous_mtime=0.0,
            current_mtime=1.0,
        )
        with pytest.raises(Exception):  # noqa: B017
            payload.profile_name = "modified"  # type: ignore[misc]

    def test_payload_to_log_dict(self) -> None:
        """to_log_dict retorna un diccionario plano con todos los campos."""
        payload = ModlistChangedPayload(
            profile_name="Test",
            modlist_path="/path",
            previous_mtime=0.0,
            current_mtime=1.0,
        )
        log_dict = payload.to_log_dict()
        assert isinstance(log_dict, dict)
        assert log_dict["profile_name"] == "Test"
        assert log_dict["previous_mtime"] == 0.0
        assert log_dict["current_mtime"] == 1.0


class TestWatcherDaemonEventBus:
    """Tests del WatcherDaemon refactorizado con EventBus."""

    async def test_watcher_publishes_event_on_change(
        self,
        watcher: WatcherDaemon,
        mock_db: AsyncMock,
        event_bus: CoreEventBus,
        tmp_path: pathlib.Path,
    ) -> None:
        """WatcherDaemon publica Event con topic system.modlist.changed."""
        received_events: list[Event] = []

        async def subscriber(event: Event) -> None:
            received_events.append(event)

        event_bus.subscribe("system.modlist.changed", subscriber)
        await event_bus.start()

        # Simular que el archivo fue modificado (mtime cambia)
        modlist_file = tmp_path / "modlist.txt"
        mock_db.get_memory.return_value = "0.0"

        # Forzar un cambio de mtime
        os.utime(str(modlist_file), (2000.0, 2000.0))

        # Iniciar watcher y esperar un ciclo
        await watcher.start()
        await asyncio.sleep(0.3)
        await watcher.stop()
        await event_bus.stop()

        # Verificar que se publicó el evento
        assert len(received_events) >= 1
        event = received_events[0]
        assert event.topic == "system.modlist.changed"
        assert event.source == "WatcherDaemon"
        assert event.payload["profile_name"] == "TestProfile"
        assert event.payload["current_mtime"] > 0.0

    async def test_watcher_no_event_without_change(
        self,
        watcher: WatcherDaemon,
        mock_db: AsyncMock,
        event_bus: CoreEventBus,
        tmp_path: pathlib.Path,
    ) -> None:
        """WatcherDaemon no publica evento si no hay cambio en mtime."""
        received_events: list[Event] = []

        async def subscriber(event: Event) -> None:
            received_events.append(event)

        event_bus.subscribe("system.modlist.changed", subscriber)
        await event_bus.start()

        # Simular que el mtime coincide (no hay cambio)
        modlist_file = tmp_path / "modlist.txt"
        current_mtime = os.stat(str(modlist_file)).st_mtime
        mock_db.get_memory.return_value = str(current_mtime)

        await watcher.start()
        await asyncio.sleep(0.3)
        await watcher.stop()
        await event_bus.stop()

        # No debe haber eventos
        assert len(received_events) == 0

    async def test_watcher_start_stop_idempotent(
        self,
        watcher: WatcherDaemon,
    ) -> None:
        """start() y stop() son idempotentes."""
        await watcher.start()
        await watcher.start()  # Segundo start no hace nada
        await watcher.stop()
        await watcher.stop()  # Segundo stop no hace nada

    async def test_watcher_updates_db_on_change(
        self,
        watcher: WatcherDaemon,
        mock_db: AsyncMock,
        event_bus: CoreEventBus,
        tmp_path: pathlib.Path,
    ) -> None:
        """WatcherDaemon actualiza el mtime en la DB al detectar cambio."""
        await event_bus.start()

        modlist_file = tmp_path / "modlist.txt"
        mock_db.get_memory.return_value = "0.0"
        os.utime(str(modlist_file), (3000.0, 3000.0))

        await watcher.start()
        await asyncio.sleep(0.3)
        await watcher.stop()
        await event_bus.stop()

        # Verificar que set_memory fue llamado con el mtime actualizado
        mock_db.set_memory.assert_called()
        call_args = mock_db.set_memory.call_args
        assert call_args[0][0] == "modlist_mtime_TestProfile"
