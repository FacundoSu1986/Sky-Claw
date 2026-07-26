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

from sky_claw.app.gui.task_tracking import (
    _BACKGROUND_TASKS,
    create_tracked_task,
    drain_tracked_tasks,
)

_GUI_DIR = pathlib.Path("sky_claw/app/gui")

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

    with caplog.at_level(logging.ERROR, logger="sky_claw.app.gui.task_tracking"):
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

    with caplog.at_level(logging.ERROR, logger="sky_claw.app.gui.task_tracking"):
        task = create_tracked_task(_sleepy(), name="gui-test-cancel")
        await asyncio.sleep(0)
        task.cancel()
        with contextlib_suppress_cancelled():
            await task
        await asyncio.sleep(0)

    # Cancellation is normal shutdown, not an error — assert on THIS module's
    # logger only (caplog.text could capture unrelated loggers' records).
    module_records = [r for r in caplog.records if r.name == "sky_claw.app.gui.task_tracking"]
    assert module_records == []
    assert task not in _BACKGROUND_TASKS


async def test_drain_espera_a_las_tasks_en_vuelo() -> None:
    """El apagado debe dejar aterrizar las escrituras cortas que ya están corriendo.

    Sin drenado, ``_cleanup`` seguía de largo y la task retomaba contra una DB ya
    cerrada: la escritura se perdía en silencio (la traga el ``except Exception``
    del handler que la lanzó).
    """
    escrituras: list[str] = []

    async def _escritura() -> None:
        await asyncio.sleep(0.05)
        escrituras.append("ok")

    create_tracked_task(_escritura(), name="gui-test-escritura")
    await drain_tracked_tasks(timeout_s=5.0)

    assert escrituras == ["ok"]
    assert not _BACKGROUND_TASKS


async def test_drain_cancela_las_que_exceden_el_timeout(caplog) -> None:
    """El apagado no puede quedar rehén de una task que no termina."""

    async def _colgada() -> None:
        await asyncio.Event().wait()

    task = create_tracked_task(_colgada(), name="gui-test-colgada")
    await asyncio.sleep(0)

    with caplog.at_level(logging.WARNING, logger="sky_claw.app.gui.task_tracking"):
        await drain_tracked_tasks(timeout_s=0.05)

    assert task.cancelled()
    # Sin truncado silencioso: el apagado nombra lo que canceló.
    assert "gui-test-colgada" in caplog.text
    assert task not in _BACKGROUND_TASKS


async def test_drain_sin_tasks_es_un_noop() -> None:
    """``asyncio.wait`` con un set vacío lanza ValueError — hay que cortar antes."""
    assert not _BACKGROUND_TASKS
    await drain_tracked_tasks(timeout_s=0.01)


async def test_drain_no_propaga_el_fallo_de_una_task() -> None:
    """Una task que revienta ya la loguea ``_on_task_done``; el drenado no debe relanzar."""

    async def _boom() -> None:
        raise RuntimeError("escritura explotó")

    create_tracked_task(_boom(), name="gui-test-drain-boom")

    await drain_tracked_tasks(timeout_s=5.0)  # no debe lanzar

    assert not _BACKGROUND_TASKS


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
