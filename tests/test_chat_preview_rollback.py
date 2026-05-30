"""P1.5 R-06 — Chat preview send must restore the input + notify on send failure.

Original ``_handle_send`` calls ``on_send_message(msg)`` and only clears the
input AFTER it succeeds. If the callback raises, the exception propagates
to NiceGUI and the user gets neither feedback nor confidence that their
message was received.

The fix is the optimistic-clear-with-rollback pattern, extracted into a
pure helper (``_try_send_with_rollback``) so it can be unit-tested without
spinning a NiceGUI runtime:

  1. Capture ``msg``.
  2. Clear the input immediately (snappy UX).
  3. Try the send callback.
  4. On exception: put ``msg`` back via ``restore_fn`` and surface the
     failure via ``notify_fn``.

Contracts:
- Successful send: notify_fn NOT called, restore_fn NOT called.
- Failing send: restore_fn called with the original text, notify_fn
  called exactly once with a human-readable error.
- BaseException (KeyboardInterrupt/SystemExit) propagates — only
  ``Exception`` is caught (we don't want to swallow shutdown signals).
"""

from __future__ import annotations

import asyncio

import pytest

from sky_claw.antigravity.gui.views.sections.chat_preview import (
    _try_send_with_rollback,
)


class TestTrySendWithRollback:
    def test_successful_send_neither_restores_nor_notifies(self) -> None:
        """Happy path: send succeeds, no rollback, no notification."""
        restore_calls: list[str] = []
        notify_calls: list[str] = []
        send_calls: list[str] = []

        _try_send_with_rollback(
            msg="hello",
            on_send=send_calls.append,
            restore_fn=restore_calls.append,
            notify_fn=notify_calls.append,
        )

        assert send_calls == ["hello"]
        assert restore_calls == [], "successful send must not trigger restore"
        assert notify_calls == [], "successful send must not notify"

    def test_failed_send_restores_text_and_notifies(self) -> None:
        """If send raises, the input is restored and the user is notified."""
        restore_calls: list[str] = []
        notify_calls: list[str] = []

        def _failing_send(_msg: str) -> None:
            raise RuntimeError("router down")

        _try_send_with_rollback(
            msg="my draft message",
            on_send=_failing_send,
            restore_fn=restore_calls.append,
            notify_fn=notify_calls.append,
        )

        assert restore_calls == ["my draft message"], (
            "Must restore the exact text the user typed, not a truncated/altered version"
        )
        assert len(notify_calls) == 1, "Must notify exactly once"
        assert "router down" in notify_calls[0] or "error" in notify_calls[0].lower()

    def test_failed_send_includes_exception_message_in_notify(self) -> None:
        """The notification must surface enough info for the user to act on."""
        notifications: list[str] = []

        def _failing_send(_msg: str) -> None:
            raise ConnectionError("nexus unreachable")

        _try_send_with_rollback(
            msg="hello",
            on_send=_failing_send,
            restore_fn=lambda _t: None,
            notify_fn=notifications.append,
        )

        assert notifications, "Notify must fire on failure"
        assert "nexus unreachable" in notifications[0]

    def test_base_exception_is_not_swallowed(self) -> None:
        """KeyboardInterrupt/SystemExit must propagate so shutdown signals work."""

        def _shutdown_send(_msg: str) -> None:
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            _try_send_with_rollback(
                msg="hello",
                on_send=_shutdown_send,
                restore_fn=lambda _t: None,
                notify_fn=lambda _t: None,
            )

    def test_original_text_preserves_whitespace_on_restore(self) -> None:
        """Copilot review #1 on PR #144: stripping must not silently drop whitespace.

        The user typed "  hello  " (with spaces). We send the stripped "hello"
        but the restored text must be the original including whitespace.
        """
        restored: list[str] = []

        def _failing_send(_msg: str) -> None:
            raise RuntimeError("down")

        _try_send_with_rollback(
            msg="hello",
            original_text="  hello  ",
            on_send=_failing_send,
            restore_fn=restored.append,
            notify_fn=lambda _t: None,
        )

        assert restored == ["  hello  "], "original_text must be restored verbatim — whitespace is user-visible"

    @pytest.mark.asyncio
    async def test_async_send_failure_triggers_rollback_via_done_callback(self) -> None:
        """Copilot review #2 on PR #144 (CRITICAL): the real GUI wiring is
        ``lambda msg: asyncio.create_task(controller.handle_send_message(msg))``.

        on_send returns a Task immediately; no exception propagates synchronously.
        Rollback must still fire when the Task later fails — otherwise the input
        is cleared optimistically and daemon errors silently lose the user's text.
        """
        restored: list[str] = []
        notifications: list[str] = []

        async def _failing_async() -> None:
            await asyncio.sleep(0)  # yield once
            raise ConnectionError("daemon offline")

        def _on_send(_msg: str) -> asyncio.Task[None]:
            return asyncio.create_task(_failing_async())

        _try_send_with_rollback(
            msg="hello",
            on_send=_on_send,
            restore_fn=restored.append,
            notify_fn=notifications.append,
        )

        # Yield long enough for the Task to fail and the done_callback to run.
        await asyncio.sleep(0.05)

        assert restored == ["hello"], (
            "Async send failure must restore the text — the optimistic clear lost it from the input"
        )
        assert notifications, "User must be notified of the async send failure"
        assert "daemon offline" in notifications[0]

    @pytest.mark.asyncio
    async def test_async_send_success_does_not_trigger_rollback(self) -> None:
        """Happy async path: Task completes successfully → no rollback, no notify."""
        restored: list[str] = []
        notifications: list[str] = []

        async def _succeeding_async() -> None:
            await asyncio.sleep(0)

        def _on_send(_msg: str) -> asyncio.Task[None]:
            return asyncio.create_task(_succeeding_async())

        _try_send_with_rollback(
            msg="hello",
            on_send=_on_send,
            restore_fn=restored.append,
            notify_fn=notifications.append,
        )
        await asyncio.sleep(0.05)

        assert restored == []
        assert notifications == []

    @pytest.mark.asyncio
    async def test_async_send_cancellation_does_not_notify_user(self) -> None:
        """A cancelled Task is part of normal shutdown — not a user-facing failure."""
        restored: list[str] = []
        notifications: list[str] = []

        async def _slow() -> None:
            await asyncio.sleep(10)

        def _on_send(_msg: str) -> asyncio.Task[None]:
            return asyncio.create_task(_slow())

        _try_send_with_rollback(
            msg="hello",
            on_send=_on_send,
            restore_fn=restored.append,
            notify_fn=notifications.append,
        )
        # Grab the task from the registry by cancelling all tasks except this one.
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        await asyncio.sleep(0.05)

        assert restored == [], "Cancellation must not trigger restore"
        assert notifications == [], "Cancellation must not surface to the user"

    def test_restore_failure_does_not_mask_original_error(self) -> None:
        """If restore_fn itself raises, the original send error info still surfaces.

        Best-effort: we should at least try to notify even if restore breaks.
        """
        notifications: list[str] = []

        def _failing_send(_msg: str) -> None:
            raise RuntimeError("primary failure")

        def _bad_restore(_text: str) -> None:
            raise RuntimeError("restore is broken too")

        # Don't crash — best-effort notification.
        _try_send_with_rollback(
            msg="hello",
            on_send=_failing_send,
            restore_fn=_bad_restore,
            notify_fn=notifications.append,
        )

        assert notifications, "Notify should still fire even if restore_fn breaks"
        assert "primary failure" in notifications[0]
