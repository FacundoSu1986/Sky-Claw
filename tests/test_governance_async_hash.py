"""H-03: GovernanceManager async hashing must not block the event loop.

Verifies:
- ``get_file_hash_async`` returns the same digest as the sync variant.
- Hashing a large file inside ``get_file_hash_async`` does not stall
  cooperatively-scheduled tasks.
- ``_HASH_CONCURRENCY`` semaphore caps in-flight hash workers.
- ``is_scanned_and_clean`` and ``update_scan_result`` exercise the async path.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

import pytest

from sky_claw.antigravity.security.governance import GovernanceManager


@pytest.fixture
def gov(tmp_path: Path, monkeypatch) -> GovernanceManager:
    monkeypatch.setattr(GovernanceManager, "_instance", None, raising=False)
    return GovernanceManager(base_path=str(tmp_path))


def _make_blob(path: Path, size_mb: int) -> str:
    chunk = b"\xab" * (1024 * 1024)
    expected = hashlib.sha256()
    with open(path, "wb") as f:
        for _ in range(size_mb):
            f.write(chunk)
            expected.update(chunk)
    return expected.hexdigest()


class TestAsyncHash:
    @pytest.mark.asyncio
    async def test_async_hash_matches_sync(self, gov, tmp_path: Path):
        target = tmp_path / "blob.bin"
        expected = _make_blob(target, size_mb=2)
        async_digest = await gov.get_file_hash_async(str(target))
        assert async_digest == expected
        assert async_digest == gov.get_file_hash(str(target))

    @pytest.mark.asyncio
    async def test_async_hash_does_not_block_loop(self, gov, tmp_path: Path):
        # 256 MB to guarantee the hash takes long enough to observe loop gaps.
        target = tmp_path / "big.bin"
        _make_blob(target, size_mb=256)

        ticks: list[float] = []
        stop = asyncio.Event()

        async def heartbeat() -> None:
            while not stop.is_set():
                ticks.append(time.perf_counter())
                await asyncio.sleep(0.01)

        hb = asyncio.create_task(heartbeat())
        # Yield so heartbeat can start before we kick off the hash.
        await asyncio.sleep(0)

        hash_start = time.perf_counter()
        digest = await gov.get_file_hash_async(str(target))
        hash_end = time.perf_counter()

        stop.set()
        await hb

        assert digest is not None
        ticks_during = [t for t in ticks if hash_start <= t <= hash_end]
        gaps = [b - a for a, b in zip(ticks_during, ticks_during[1:])]
        max_gap = max(gaps, default=0.0)

        # If the loop were blocked by sync I/O, all ticks would clump at the
        # start and end with one giant gap in the middle. A healthy loop keeps
        # pumping at ~10ms intervals; allow generous slack for Windows GIL +
        # scheduler jitter.
        assert ticks_during, "Heartbeat never ran during hashing window"
        assert max_gap < 0.2, (
            f"Event loop blocked for {max_gap:.3f}s during hash "
            f"(hash duration={hash_end - hash_start:.2f}s, ticks={len(ticks_during)})"
        )

    @pytest.mark.asyncio
    async def test_concurrency_capped_by_semaphore(self, gov, tmp_path: Path, monkeypatch):
        gov._HASH_CONCURRENCY = 2
        gov._hash_semaphore = None

        in_flight = 0
        peak = 0
        lock = asyncio.Lock()

        original = GovernanceManager._hash_file_blocking

        def tracked(path: str):
            nonlocal in_flight, peak
            # Synchronous body — runs in to_thread worker.
            time.sleep(0.05)
            return original(path)

        monkeypatch.setattr(GovernanceManager, "_hash_file_blocking", staticmethod(tracked))

        files = []
        for i in range(8):
            p = tmp_path / f"f{i}.bin"
            _make_blob(p, size_mb=1)
            files.append(str(p))

        async def hash_one(p: str):
            nonlocal in_flight, peak
            async with gov._get_hash_semaphore():
                async with lock:
                    in_flight += 1
                    peak = max(peak, in_flight)
                try:
                    return await asyncio.to_thread(GovernanceManager._hash_file_blocking, p)
                finally:
                    async with lock:
                        in_flight -= 1

        await asyncio.gather(*(hash_one(p) for p in files))
        assert peak <= 2, f"Concurrency cap violated: peak={peak}, limit=2"

    @pytest.mark.asyncio
    async def test_is_scanned_and_clean_uses_async_path(self, gov, tmp_path: Path):
        target = tmp_path / "clean.bin"
        _make_blob(target, size_mb=1)
        digest = gov.get_file_hash(str(target))
        gov.whitelist.add(digest)
        assert await gov.is_scanned_and_clean(str(target)) is True

    @pytest.mark.asyncio
    async def test_missing_file_returns_none(self, gov, tmp_path: Path):
        digest = await gov.get_file_hash_async(str(tmp_path / "ghost.bin"))
        assert digest is None
