"""
Tests de estrés para verificar la resiliencia del SyncEngine.

Tests:
1. test_ten_consecutive_download_failures - 10 fallos consecutivos de descarga
2. test_network_timeouts - Timeouts de red
3. test_memory_exceptions - Excepciones de memoria
4. test_orchestrator_remains_operational_after_all_failures - El orquestador permanece operativo
"""

import asyncio
import pytest
from tenacity import wait_none
from unittest.mock import AsyncMock
import sys
import os

# Add path to sky_claw module
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sky_claw.orchestrator.sync_engine import SyncEngine, SyncConfig


class TestSyncEngineResilience:
    """Tests de estrés para verificar la resiliencia del SyncEngine."""

    @pytest.fixture
    def mock_registry(self):
        """Mock del AsyncModRegistry."""
        registry = AsyncMock()
        registry.log_tasks_batch.return_value = None
        return registry

    @pytest.fixture
    def mock_controller(self):
        """Mock del MO2Controller."""
        return AsyncMock()

    @pytest.fixture
    def mock_client(self):
        """Mock del MasterlistClient."""
        return AsyncMock()

    @pytest.fixture
    def mock_downloader(self):
        """Mock del NexusDownloader."""
        return AsyncMock()

    @pytest.fixture
    def mock_hitl(self):
        """Mock del HITLGuard."""
        return AsyncMock()

    @pytest.fixture
    def sync_engine(
        self, mock_registry, mock_controller, mock_client, mock_downloader, mock_hitl
    ):
        """Fixture que proporciona SyncEngine con todos los mocks."""
        engine = SyncEngine(
            mo2=mock_controller,
            masterlist=mock_client,
            registry=mock_registry,
            downloader=mock_downloader,
            hitl=mock_hitl,
            config=SyncConfig(worker_count=2, batch_size=5),
            fetch_retry_wait=wait_none(),
        )
        return engine

    @pytest.mark.asyncio
    async def test_ten_consecutive_download_failures(self, sync_engine, mock_registry):
        """Test: 10 fallos consecutivos de descarga no causan crash."""
        call_count = 0

        async def failing_download():
            nonlocal call_count
            call_count += 1
            raise ConnectionError("Simulated network error")

        # Launch 10 failing downloads
        for i in range(10):
            sync_engine.enqueue_download(failing_download(), context=f"download_{i}")

        # Wait for all tasks to complete
        await asyncio.sleep(0.5)

        # Verify all tasks executed
        assert call_count == 10

        # Verify metrics updated
        assert await sync_engine.metrics.get_error_count() == 10

    @pytest.mark.asyncio
    async def test_network_timeouts(self, sync_engine, mock_registry):
        """Test: Timeouts de red no causan crash."""

        async def timeout_download():
            await asyncio.sleep(0.1)  # Simulate timeout
            raise asyncio.TimeoutError("Simulated timeout")

        tasks = [timeout_download() for _ in range(5)]

        for task in tasks:
            sync_engine.enqueue_download(task, context="timeout_test")

        await asyncio.sleep(0.3)

        # Verify errors captured
        assert await sync_engine.metrics.get_error_count() >= 5

    @pytest.mark.asyncio
    async def test_memory_exceptions(self, sync_engine, mock_registry):
        """Test: MemoryError no causa crash."""

        async def memory_intensive_download():
            raise MemoryError("Simulated out of memory")

        tasks = [memory_intensive_download() for _ in range(3)]

        for task in tasks:
            sync_engine.enqueue_download(task, context="memory_test")

        await asyncio.sleep(0.3)

        # Verify errors captured
        assert await sync_engine.metrics.get_error_count() >= 3

    @pytest.mark.asyncio
    async def test_orchestrator_remains_operational_after_all_failures(
        self, sync_engine, mock_registry
    ):
        """Test: El orquestador permanece operativo después de todos los fallos."""

        # Simulate multiple error types
        async def error_type_1():
            raise ValueError("Error type 1")

        async def error_type_2():
            raise RuntimeError("Error type 2")

        async def error_type_3():
            raise KeyError("Error type 3")

        tasks = [error_type_1(), error_type_2(), error_type_3()]

        for task in tasks:
            sync_engine.enqueue_download(task, context="mixed_errors")

        await asyncio.sleep(0.3)

        # Verify orchestrator still works
        assert await sync_engine.metrics.get_error_count() >= 3

        # Verify error types tracked
        error_types = await sync_engine.metrics.get_error_types()
        assert "ValueError" in error_types
        assert "RuntimeError" in error_types
        assert "KeyError" in error_types


if __name__ == "__main__":
    pytest.main()
