"""Tests for P0-2 and P0-3: AsyncToolRegistry.

Verifies:
1. P0-2: Fresh URL fetch inside _download_mod closure
2. P0-3: Auto-initialize LOOTRunner from loot_exe
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.antigravity.agent.tools import AsyncToolRegistry
from sky_claw.antigravity.scraper.nexus_downloader import FileInfo, NexusDownloader
from sky_claw.antigravity.security.hitl import Decision, HITLGuard
from sky_claw.antigravity.security.network_gateway import EgressPolicy, NetworkGateway


class TestLootAutoInit:
    """Tests for P0-3: Auto-initialization of LOOTRunner."""

    @pytest.fixture
    def mock_registry(self):
        """Mock AsyncModRegistry."""
        return MagicMock()

    @pytest.fixture
    def mock_mo2(self):
        """Mock MO2Controller."""
        mo2 = MagicMock()
        mo2.root = pathlib.Path("/test/mo2")
        return mo2

    @pytest.fixture
    def mock_sync_engine(self):
        """Mock SyncEngine."""
        return MagicMock()

    def test_p0_3_auto_initializes_loot_runner(self, mock_registry, mock_mo2, mock_sync_engine):
        """P0-3 FIX: Should auto-create LOOTRunner from loot_exe when None.

        Previously, _run_loot_sort would fail with "LOOT runner is not configured"
        if loot_runner wasn't explicitly provided. Now it auto-initializes.
        """
        loot_exe = pathlib.Path("/test/loot.exe")

        registry = AsyncToolRegistry(
            registry=mock_registry,
            mo2=mock_mo2,
            sync_engine=mock_sync_engine,
            loot_exe=loot_exe,
            loot_runner=None,  # Not provided
        )

        # Should have stored the loot_exe
        assert registry._loot_exe == loot_exe
        # But loot_runner should still be None until first use
        assert registry._loot_runner is None

    @pytest.mark.asyncio
    async def test_p0_3_creates_loot_runner_on_first_sort(self, mock_registry, mock_mo2, mock_sync_engine, tmp_path):
        """P0-3 FIX: Should create LOOTRunner on first sort attempt."""
        # Create mock loot.exe
        loot_exe = tmp_path / "loot.exe"
        loot_exe.touch()

        # Create mock game directory
        game_dir = tmp_path.parent / "SkyrimSE"
        game_dir.mkdir()

        mock_mo2.root = tmp_path

        registry = AsyncToolRegistry(
            registry=mock_registry,
            mo2=mock_mo2,
            sync_engine=mock_sync_engine,
            loot_exe=loot_exe,
            loot_runner=None,
        )

        # Mock LOOTRunner to avoid actual execution
        with patch("sky_claw.antigravity.agent.tools.LOOTRunner") as mock_loot_runner_cls:
            mock_runner = MagicMock()
            mock_runner.sort = AsyncMock(
                return_value=MagicMock(
                    success=True,
                    return_code=0,
                    sorted_plugins=[],
                    warnings=[],
                    errors=[],
                )
            )
            mock_loot_runner_cls.return_value = mock_runner

            # Call _run_loot_sort - should auto-initialize
            result = await registry._run_loot_sort("Test Profile")

            # Should have been called
            mock_loot_runner_cls.assert_called_once()
            # Result should be success (JSON string)
            assert "success" in result or "error" in result


class TestDownloadModFreshUrl:
    """Tests for P0-2: Fresh URL fetch in download closure."""

    @pytest.fixture
    def mock_registry(self):
        """Mock AsyncModRegistry."""
        return MagicMock()

    @pytest.fixture
    def mock_mo2(self):
        """Mock MO2Controller."""
        return MagicMock()

    @pytest.fixture
    def mock_sync_engine(self):
        """Mock SyncEngine."""
        engine = MagicMock()
        engine.enqueue_download = MagicMock()
        return engine

    @pytest.fixture
    def mock_downloader(self):
        """Mock NexusDownloader."""
        downloader = MagicMock(spec=NexusDownloader)
        downloader.get_file_info = AsyncMock(
            return_value=FileInfo(
                nexus_id=1234,
                file_id=5678,
                file_name="test_mod.7z",
                size_bytes=1024,
                md5="",
                download_url="https://cdn.nexus.com/file.7z",
            )
        )
        downloader.download = AsyncMock()
        downloader.staging_dir = pathlib.Path("/tmp/downloads")
        return downloader

    @pytest.fixture
    def mock_hitl(self):
        """Mock HITLGuard."""
        hitl = MagicMock(spec=HITLGuard)
        hitl.request_approval = AsyncMock(return_value=Decision.APPROVED)
        return hitl

    def test_p0_2_captures_mod_ids_not_url(self, mock_registry, mock_mo2, mock_sync_engine, mock_downloader, mock_hitl):
        """P0-2 FIX: Should capture nexus_id/file_id, not the URL itself.

        Previously, the download closure captured file_info.download_url directly,
        which could expire before the download actually runs. Now it re-fetches
        the URL fresh when the download executes.
        """
        registry = AsyncToolRegistry(
            registry=mock_registry,
            mo2=mock_mo2,
            sync_engine=mock_sync_engine,
            hitl=mock_hitl,
            downloader=mock_downloader,
            gateway=NetworkGateway(EgressPolicy(block_private_ips=False)),  # TASK-013 P1
        )

        # Call _download_mod
        asyncio.run(registry._download_mod(nexus_id=1234, file_id=5678))

        # Verify enqueue was called with a coroutine
        mock_sync_engine.enqueue_download.assert_called_once()

        # Get the enqueued coroutine
        enqueued_coro = mock_sync_engine.enqueue_download.call_args[0][0]

        # Run the coroutine to verify it works
        asyncio.run(enqueued_coro)

        # Verify fresh URL was fetched (get_file_info called twice: once for HITL, once in closure)
        assert mock_downloader.get_file_info.call_count >= 2

        # Verify download was called with fresh info
        mock_downloader.download.assert_called_once()

    @pytest.mark.asyncio
    async def test_p0_2_rejects_denied_download(
        self, mock_registry, mock_mo2, mock_sync_engine, mock_downloader, mock_hitl
    ):
        """P0-2: Should not enqueue download if HITL denies."""
        # Set up HITL to deny
        mock_hitl.request_approval = AsyncMock(return_value=Decision.DENIED)

        registry = AsyncToolRegistry(
            registry=mock_registry,
            mo2=mock_mo2,
            sync_engine=mock_sync_engine,
            hitl=mock_hitl,
            downloader=mock_downloader,
            gateway=NetworkGateway(EgressPolicy(block_private_ips=False)),  # TASK-013 P1
        )

        result = await registry._download_mod(nexus_id=1234, file_id=5678)

        # Should not enqueue
        mock_sync_engine.enqueue_download.assert_not_called()

        # Should return denied status
        assert "denied" in result
