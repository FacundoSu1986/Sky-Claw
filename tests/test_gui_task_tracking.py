"""PR-4 (obs #211): GUI button lambdas must not use bare ``asyncio.create_task``.

A bare ``create_task`` in a button/switch lambda keeps no strong reference (the
task can be garbage-collected mid-flight — the risk ws_daemon.py documents) and
swallows exceptions until the task destructor. ``create_tracked_task`` keeps the
task in a module-level set and logs failures through the structured pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib

from sky_claw.antigravity.gui.task_tracking import _BACKGROUND_TASKS, create_tracked_task

_GUI_DIR = pathlib.Path("sky_claw/antigravity/gui")

#: Files migrated in PR-4 — bare create_task must not reappear in them.
_MIGRATED_FILES = [
    _GUI_DIR / "views" / "actions.py",
    _GUI_DIR / "views" / "mod_list.py",
    _GUI_DIR / "sky_claw_gui.py",
]


async def test_tracked_task_holds_strong_ref_until_done():
    started = asyncio.Event()
    release = asyncio.Event()

    async def _work() -> str:
        started.set()
        await release.wait()
        return "done"

    task = create_tracked_task(_work(), name="gui-test-work")
    await started.wait()
    assert task in _BACKGROUND_TASKS  # strong ref held while running

    release.set()
    assert await task == "done"
    await asyncio.sleep(0)  # let the done callback run
    assert task not in _BACKGROUND_TASKS  # no leak after completion


async def test_tracked_task_logs_exception(caplog):
    async def _boom() -> None:
        raise RuntimeError("button handler exploded")

    with caplog.at_level(logging.ERROR, logger="sky_claw.antigravity.gui.task_tracking"):
        task = create_tracked_task(_boom(), name="gui-test-boom")
        with contextlib_suppress_runtime():
            await task
        await asyncio.sleep(0)

    assert "gui-test-boom" in caplog.text
    assert "button handler exploded" in caplog.text
    assert task not in _BACKGROUND_TASKS


async def test_tracked_task_cancellation_is_silent(caplog):
    async def _sleepy() -> None:
        await asyncio.sleep(3600)

    with caplog.at_level(logging.ERROR, logger="sky_claw.antigravity.gui.task_tracking"):
        task = create_tracked_task(_sleepy(), name="gui-test-cancel")
        await asyncio.sleep(0)
        task.cancel()
        with contextlib_suppress_cancelled():
            await task
        await asyncio.sleep(0)

    # Cancellation is normal shutdown, not an error — assert on THIS module's
    # logger only (caplog.text could capture unrelated loggers' records).
    module_records = [r for r in caplog.records if r.name == "sky_claw.antigravity.gui.task_tracking"]
    assert module_records == []
    assert task not in _BACKGROUND_TASKS


def test_no_bare_create_task_in_migrated_gui_files():
    """Regression guard: RUF006 does not flag ``create_task`` inside lambdas,
    so this enforces the helper in the files PR-4 migrated."""
    offenders = [str(f) for f in _MIGRATED_FILES if "asyncio.create_task(" in f.read_text(encoding="utf-8")]
    assert offenders == [], f"bare asyncio.create_task in GUI files: {offenders}"


# --- tiny local helpers (keep asserts above readable) -------------------------


def contextlib_suppress_runtime():
    import contextlib

    return contextlib.suppress(RuntimeError)


def contextlib_suppress_cancelled():
    import contextlib

    return contextlib.suppress(asyncio.CancelledError)
