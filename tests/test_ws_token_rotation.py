"""Tests for WS token rotation invalidation (F3).

Verifies the AuthTokenManager rotation-callback machinery:

  1. register_rotation_callback() stores callbacks in AuthTokenManager.
  2. _rotation_loop() invokes registered callbacks after generate() succeeds
     and skips them when generate() raises.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from sky_claw.antigravity.security.auth_token_manager import AuthTokenManager

# ---------------------------------------------------------------------------
# AuthTokenManager — callback registry
# ---------------------------------------------------------------------------


class TestRotationCallbackRegistry:
    @pytest.fixture(autouse=True)
    def bypass_token_dir_permissions(self):
        with patch("sky_claw.antigravity.security.auth_token_manager.restrict_to_owner"):
            yield

    def test_register_single_callback(self, tmp_path):
        mgr = AuthTokenManager(token_dir=str(tmp_path))
        cb = AsyncMock()
        mgr.register_rotation_callback(cb)
        assert cb in mgr._rotation_callbacks

    def test_register_multiple_callbacks(self, tmp_path):
        mgr = AuthTokenManager(token_dir=str(tmp_path))
        cb1, cb2 = AsyncMock(), AsyncMock()
        mgr.register_rotation_callback(cb1)
        mgr.register_rotation_callback(cb2)
        assert mgr._rotation_callbacks == [cb1, cb2]

    def test_register_same_callback_twice_is_idempotent(self, tmp_path):
        """Registering the same callable twice must not duplicate it."""
        mgr = AuthTokenManager(token_dir=str(tmp_path))
        cb = AsyncMock()
        mgr.register_rotation_callback(cb)
        mgr.register_rotation_callback(cb)
        assert mgr._rotation_callbacks.count(cb) == 1

    @pytest.mark.asyncio
    async def test_rotation_loop_calls_callbacks_on_success(self, tmp_path):
        """After generate() succeeds, all registered callbacks are awaited."""
        mgr = AuthTokenManager(token_dir=str(tmp_path))
        cb = AsyncMock()
        mgr.register_rotation_callback(cb)

        # First sleep completes normally → iteration runs → callbacks fire.
        # Second sleep raises CancelledError → loop exits.
        sleep_calls = 0

        async def sleep_once_then_cancel(_):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError

        with (
            patch.object(mgr, "generate", return_value="tok"),
            patch("asyncio.sleep", side_effect=sleep_once_then_cancel),
            pytest.raises(asyncio.CancelledError),
        ):
            await mgr._rotation_loop()

        cb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rotation_loop_skips_callbacks_on_generate_failure(self, tmp_path):
        """When generate() raises, callbacks must NOT be called."""
        mgr = AuthTokenManager(token_dir=str(tmp_path))
        cb = AsyncMock()
        mgr.register_rotation_callback(cb)

        sleep_calls = 0

        async def sleep_once_then_cancel(_):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError

        with (
            patch.object(mgr, "generate", side_effect=RuntimeError("disk full")),
            patch("asyncio.sleep", side_effect=sleep_once_then_cancel),
            pytest.raises(asyncio.CancelledError),
        ):
            await mgr._rotation_loop()

        cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rotation_loop_continues_if_callback_raises(self, tmp_path):
        """A callback that raises must not break the rotation loop; subsequent callbacks still run."""
        mgr = AuthTokenManager(token_dir=str(tmp_path))
        bad_cb = AsyncMock(side_effect=RuntimeError("cb failed"))
        good_cb = AsyncMock()
        mgr.register_rotation_callback(bad_cb)
        mgr.register_rotation_callback(good_cb)

        sleep_calls = 0

        async def sleep_once_then_cancel(_):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError

        with (
            patch.object(mgr, "generate", return_value="tok"),
            patch("asyncio.sleep", side_effect=sleep_once_then_cancel),
            pytest.raises(asyncio.CancelledError),
        ):
            await mgr._rotation_loop()

        good_cb.assert_awaited_once()
