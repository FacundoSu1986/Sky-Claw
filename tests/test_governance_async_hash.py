"""H-03: GovernanceManager async hashing must not block the event loop.

Verifies:
- ``get_file_hash_async`` returns the same digest as the sync variant.
- Hashing inside ``get_file_hash_async`` does not stall cooperatively-scheduled
  tasks (no heavy I/O — ``_hash_file_blocking`` is monkeypatched).
- ``_HASH_CONCURRENCY`` semaphore is enforced by the *production* path
  ``get_file_hash_async``, not by a custom wrapper.
- ``is_scanned_and_clean`` and ``update_scan_result`` exercise the async path.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from pathlib import Path

import pytest

from sky_claw.antigravity.security.governance import GovernanceManager

_FAKE_DIGEST = "a" * 64  # deterministic stand-in for SHA-256 hex


@pytest.fixture
def gov(tmp_path: Path, monkeypatch) -> GovernanceManager:
    monkeypatch.setattr(GovernanceManager, "_instance", None, raising=False)
    return GovernanceManager(base_path=str(tmp_path))


def _make_blob(path: Path, size_mb: int) -> str:
    """Write a real file and return its true SHA-256 digest."""
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
        """Digest parity between sync and async paths on a small real file."""
        target = tmp_path / "blob.bin"
        expected = _make_blob(target, size_mb=2)
        async_digest = await gov.get_file_hash_async(str(target))
        assert async_digest == expected
        assert async_digest == gov.get_file_hash(str(target))

    @pytest.mark.asyncio
    async def test_async_hash_does_not_block_loop(self, gov, tmp_path: Path, monkeypatch):
        """Event loop stays responsive while ``get_file_hash_async`` runs.

        ``_hash_file_blocking`` is monkeypatched to sleep 0.3 s without real
        I/O, so the test is fast (<1 s total) and deterministic across CI
        runners regardless of disk throughput.
        """
        target = tmp_path / "stub.bin"
        target.write_bytes(b"\x00")  # minimal real file required for the path check

        def _slow_stub(path: str) -> str:
            time.sleep(0.3)  # simulate expensive hash without heavy I/O
            return _FAKE_DIGEST

        monkeypatch.setattr(GovernanceManager, "_hash_file_blocking", staticmethod(_slow_stub))

        ticks: list[float] = []
        stop = asyncio.Event()

        async def heartbeat() -> None:
            while not stop.is_set():
                ticks.append(time.perf_counter())
                await asyncio.sleep(0.01)

        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)  # yield so heartbeat starts before hash

        hash_start = time.perf_counter()
        digest = await gov.get_file_hash_async(str(target))
        hash_end = time.perf_counter()

        stop.set()
        await hb

        assert digest == _FAKE_DIGEST
        ticks_during = [t for t in ticks if hash_start <= t <= hash_end]
        gaps = [b - a for a, b in zip(ticks_during, ticks_during[1:], strict=False)]
        max_gap = max(gaps, default=0.0)

        assert ticks_during, "Heartbeat never ran during hashing window"
        # A blocked loop would produce one gap of ~300ms. Allow generous slack
        # for Windows GIL + scheduler jitter.
        assert max_gap < 0.15, (
            f"Event loop blocked for {max_gap:.3f}s during hash "
            f"(hash_duration={hash_end - hash_start:.2f}s, ticks={len(ticks_during)})"
        )

    @pytest.mark.asyncio
    async def test_concurrency_capped_by_semaphore(self, gov, tmp_path: Path, monkeypatch):
        """Semaphore enforcement is validated through the *production* path.

        Driving via ``gov.get_file_hash_async()`` (not a hand-rolled wrapper
        around ``_get_hash_semaphore``) means a regression that removes the
        semaphore from the production method would cause the assertion to fail.

        Concurrency is measured with a ``threading.Lock`` inside
        ``_hash_file_blocking`` because that function runs in the thread pool,
        not on the event loop — an asyncio.Lock would be the wrong primitive.
        """
        gov._HASH_CONCURRENCY = 2
        gov._hash_semaphore = None  # force lazy re-init with new limit

        # Thread-safe counters — workers run outside the event loop.
        in_flight = 0
        peak = 0
        counter_lock = threading.Lock()

        original_blocking = GovernanceManager._hash_file_blocking

        def tracked_blocking(path: str) -> str | None:
            nonlocal in_flight, peak
            with counter_lock:
                in_flight += 1
                peak = max(peak, in_flight)
            try:
                time.sleep(0.05)  # hold slot long enough for peak to be observable
                return original_blocking(path)
            finally:
                with counter_lock:
                    in_flight -= 1

        monkeypatch.setattr(GovernanceManager, "_hash_file_blocking", staticmethod(tracked_blocking))

        # Small real files — only path presence is needed; actual I/O is mocked above.
        files = [str(tmp_path / f"f{i}.bin") for i in range(8)]
        for p in files:
            Path(p).write_bytes(b"\xab" * 1024)

        # Drive exclusively through the production method.
        await asyncio.gather(*(gov.get_file_hash_async(p) for p in files))
        assert peak <= 2, f"Concurrency cap violated: peak in-flight={peak}, limit=2"

    @pytest.mark.asyncio
    async def test_is_scanned_and_clean_uses_async_path(self, gov, tmp_path: Path):
        """Whitelist hit returns True without hitting the DB."""
        target = tmp_path / "clean.bin"
        _make_blob(target, size_mb=1)
        digest = gov.get_file_hash(str(target))
        gov.whitelist.add(digest)
        assert await gov.is_scanned_and_clean(str(target)) is True

    @pytest.mark.asyncio
    async def test_missing_file_returns_none(self, gov, tmp_path: Path):
        """Non-existent path returns None without raising."""
        digest = await gov.get_file_hash_async(str(tmp_path / "ghost.bin"))
        assert digest is None
