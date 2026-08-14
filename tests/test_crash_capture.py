"""Contratos de captura terminal para tareas y subprocesos."""

from __future__ import annotations

import asyncio
import hashlib
import logging

import pytest

from sky_claw.app_context import AppContext
from sky_claw.logging_config import subprocess_error_extra


async def test_track_task_registra_excepcion_y_la_consume(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = AppContext.__new__(AppContext)
    ctx._background_tasks = set()

    async def _fallar() -> None:
        raise RuntimeError("task rota")

    with caplog.at_level(logging.ERROR, logger="sky_claw"):
        task = ctx._track_task(_fallar(), name="langgraph-background")
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    record = next(item for item in caplog.records if getattr(item, "event", "") == "background_task_failed")
    assert record.task_name == "langgraph-background"
    assert record.exc_info is not None
    assert "task rota" in caplog.text
    assert task not in ctx._background_tasks


async def test_track_task_no_reporta_cancelacion_normal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = AppContext.__new__(AppContext)
    ctx._background_tasks = set()
    started = asyncio.Event()

    async def _esperar() -> None:
        started.set()
        await asyncio.Event().wait()

    task = ctx._track_task(_esperar(), name="cancelacion-normal")
    await started.wait()
    with caplog.at_level(logging.ERROR, logger="sky_claw"):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert not any(getattr(item, "event", "") == "background_task_failed" for item in caplog.records)


def test_stderr_completo_hasta_limite_y_tail_verificable() -> None:
    breve = "detalle completo"
    assert subprocess_error_extra(
        operation="loot_sort",
        tool="LOOT",
        exit_code=1,
        stderr=breve,
    ) == {
        "event": "external_process_failed",
        "operation": "loot_sort",
        "tool": "LOOT",
        "exit_code": 1,
        "stderr": breve,
        "stderr_size": len(breve.encode()),
        "stderr_sha256": hashlib.sha256(breve.encode()).hexdigest(),
        "stderr_truncated": False,
    }

    largo = "inicio-secreto-" + ("x" * (64 * 1024)) + "-tail"
    extra = subprocess_error_extra(
        operation="xedit_read_only",
        tool="xEdit",
        exit_code=17,
        stderr=largo,
        job_id="job-7",
        child_pid=123,
    )
    assert extra["stderr_truncated"] is True
    assert str(extra["stderr"]).endswith("-tail")
    assert len(str(extra["stderr"]).encode()) <= 64 * 1024
    assert extra["stderr_size"] == len(largo.encode())
    assert extra["stderr_sha256"] == hashlib.sha256(largo.encode()).hexdigest()
    assert extra["job_id"] == "job-7"
    assert extra["child_pid"] == 123


def test_tx_id_distingue_no_suministrado_de_suministrado_valiendo_none() -> None:
    """ "No lo pasaron" y "lo pasaron y no hay transacción" NO son lo mismo.

    ``tx_id`` no puede seguir la regla de los demás opcionales ("si es None, se
    omite") sin romper la invariante que ``dyndolod_runner.py`` promete: TODO su
    registro de fallo lleva la clave, con ``None`` como valor honesto para "acá
    no hay transacción". Con la regla de omisión, los dos registros del runner
    que usan este helper —que son, encima, dos de los que SÍ llevan
    ``pipeline_stage``— perdían la clave entera al correr sin servicio, mientras
    los otros 23 la llevaban como ``None``. Medido, no supuesto: un
    ``run_dyndolod()`` directo dejaba `tiene_tx_id=False` justo en el registro
    con la etapa (review Qodo, PR #471, "Correlación rota en runtime").

    Eso reintroducía exactamente la excepción que el PR #464 evitó a propósito
    cuando declaró el ``tx_id`` del servicio antes del preflight: "un valor
    declarado valiendo None es un valor honesto para 'sin TX', más barato que
    tallar una excepción que un test tenga que conocer".

    El centinela lo resuelve sin tocar a nadie más: los tres módulos que usan
    este helper sin participar del pipeline (`loot/cli.py`, `xedit/runner.py`,
    `mo2/vfs_worker.py`) no pasan el argumento, así que su dict sigue idéntico
    —lo congela `test_stderr_completo_hasta_limite_y_tail_verificable`, acá
    arriba— y quien lo pasa explícito recibe la clave aunque valga ``None``.
    """
    comunes = {"operation": "run_dyndolod", "tool": "DynDOLOD", "exit_code": 1, "stderr": ""}

    assert "tx_id" not in subprocess_error_extra(**comunes), (
        "un llamador que no participa del pipeline no debe empezar a emitir tx_id"
    )

    explicito_none = subprocess_error_extra(**comunes, tx_id=None)
    assert "tx_id" in explicito_none, (
        "pasar tx_id=None es declarar 'no hay transacción acá', no omitir el campo: "
        "sin la clave, el registro con pipeline_stage del runner queda sin correlación "
        "mientras sus 23 hermanos la llevan como None."
    )
    assert explicito_none["tx_id"] is None

    assert subprocess_error_extra(**comunes, tx_id=42)["tx_id"] == 42
