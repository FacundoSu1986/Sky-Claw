"""P0 regression: external-tool runners must never leak an orphaned OS process.

BodySlide / Pandora / Wrye Bash are long-running GUI tools that frequently hang.
When a run times out OR the orchestrator is shut down (task cancellation), the
runner must ``kill()`` + reap the child process. Otherwise the orphan keeps
holding handles on the MO2 VFS and the Skyrim ``Data`` directory, which breaks
every subsequent run in an unattended/autonomous session.

These tests intentionally fail against the pre-fix code (the ``except`` blocks
returned a failure result without killing the process).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sky_claw.local.tools._process as proceso_mod
from sky_claw.local.tools.bodyslide_runner import (
    BodySlideConfig,
    BodySlideExecutionError,
    BodySlideRunner,
    BodySlideTimeoutError,
)
from sky_claw.local.tools.pandora_runner import (
    PandoraConfig,
    PandoraExecutionError,
    PandoraRunner,
    PandoraTimeoutError,
)
from sky_claw.local.tools.wrye_bash_runner import (
    WryeBashExecutionError,
    WryeBashTimeoutError,
)


@pytest.fixture
def job_simulado(monkeypatch: pytest.MonkeyPatch) -> None:
    """El proceso doble no tiene un PID real: Windows no puede meterlo en un Job.

    BodySlide exige ``require_job=True`` (cierre #1003) y, sin Job, ``run_capture``
    falla cerrado ANTES de esperar al hijo — en un runner Windows este test
    observaría ese fallo y no el kill por timeout/cancelación que protege. Se
    simula la asignación (y el cierre) del Job: el contrato del Job en sí lo
    prueban ``test_process_helper.py`` y ``test_bodyslide_cierre_contractual.py``.
    """
    monkeypatch.setattr(proceso_mod, "assign_kill_on_close_job", lambda _pid: 1)
    monkeypatch.setattr(proceso_mod, "close_job", lambda _job: None)


def _hanging_proc() -> AsyncMock:
    """A subprocess mock whose ``communicate()`` never returns (a hung GUI tool).

    ``kill()`` on a real :class:`asyncio.subprocess.Process` is synchronous, so
    it is a plain :class:`MagicMock`; ``wait()`` is awaited, so it is an
    :class:`AsyncMock`.
    """
    proc = AsyncMock()

    async def _never(*_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
        await asyncio.sleep(3600)
        return (b"", b"")

    proc.communicate = AsyncMock(side_effect=_never)
    proc.wait = AsyncMock(return_value=-9)
    proc.kill = MagicMock()
    proc.returncode = None
    return proc


def _bodyslide(tmp_path, timeout: float) -> tuple[BodySlideRunner, tuple]:
    # El factory necesita un Config.xml con TargetGame válido y quiet_verified=True
    # para que el runner pase el gate de capacidad y la siembra (P0-1, P1-5) —
    # sin eso el runner aborta con CAPABILITY_UNKNOWN antes del spawn y el
    # timeout/cancel never happen (el kill de este test no tendría nada que
    # matar).
    (tmp_path / "Config.xml").write_text("<Config>\n  <TargetGame>4</TargetGame>\n</Config>\n", encoding="utf-8")
    runner = BodySlideRunner(
        BodySlideConfig(
            bodyslide_exe=tmp_path / "BodySlide.exe",
            game_path=tmp_path,
            timeout_seconds=timeout,
            quiet_verified=True,
        )
    )
    # El target debe ser el destino canónico DEL GRUPO (P0-C + P1-A, revisión #1003).
    return runner, ("Build", str(tmp_path / "BodySlide_Output" / "Build"))


def _pandora(tmp_path, timeout: float) -> tuple[PandoraRunner, tuple]:
    runner = PandoraRunner(
        PandoraConfig(pandora_exe=tmp_path / "Pandora.exe", game_path=tmp_path, timeout_seconds=timeout)
    )
    return runner, ()


#: Runners que efectivamente lanzan un proceso hijo y por lo tanto pueden dejar
#: un huérfano. Wrye Bash quedó FUERA a propósito: verificado contra
#: `wrye-bash/wrye-bash` `Mopy/bash/barg.py`, Wrye Bash no expone ningún flag que
#: construya el Bashed Patch sin GUI, así que `generate_bashed_patch` ahora falla
#: cerrado con `WryeBashHeadlessUnsupportedError` sin spawnear (antes corría
#: `-b`, que en el parser real es el switch `--backup` de settings y no toma
#: valor: la invocación moría en `argparse` sin causa legible).
#: Sin proceso hijo no hay huérfano que prevenir. Si la etapa 6 vuelve a lanzar
#: un proceso —p. ej. al modelarla como etapa asistida— hay que agregar acá su
#: factory y su excepción de timeout dedicada.
_FACTORIES_QUE_SPAWNEAN = [_bodyslide, _pandora]

_CALLS = {
    BodySlideRunner: lambda r, a: r.run_batch(*a),
    PandoraRunner: lambda r, a: r.run_pandora(*a),
}

#: U-10: cada runner eleva SU excepción de timeout dedicada (ya no traga struct).
_TIMEOUT_EXC = {
    BodySlideRunner: BodySlideTimeoutError,
    PandoraRunner: PandoraTimeoutError,
}


def test_timeout_errors_derivan_de_su_execution_error() -> None:
    """U-10: la Timeout dedicada deriva de la ExecutionError base del runner.

    Así los callers que ya capturan la base (``wrye_bash_service`` /
    ``pandora_service`` con ``except *ExecutionError``; ``system_tools`` con
    ``except Exception``) la traducen al contrato ``success=False`` sin cambios,
    mientras el tipo dedicado permite distinguir el timeout y —al PROPAGARSE en
    vez de retornar struct— habilita el rollback transaccional de U-04.
    """
    assert issubclass(WryeBashTimeoutError, WryeBashExecutionError)
    assert issubclass(BodySlideTimeoutError, BodySlideExecutionError)
    assert issubclass(PandoraTimeoutError, PandoraExecutionError)


@pytest.mark.parametrize("factory", _FACTORIES_QUE_SPAWNEAN)
async def test_runner_kills_process_on_timeout(tmp_path, factory, job_simulado):
    """En timeout, el runner debe matar+reapear el proceso hijo Y elevar su
    excepción de timeout dedicada (U-10).

    Antes el ``except TimeoutError`` retornaba un ``*Result`` con ``success=False``
    y el ``async with`` del lock/rollback salía limpio (el rollback nunca se
    disparaba). Ahora eleva su ``*TimeoutError``, que se propaga por el context
    manager — sin dejar de matar el proceso colgado (el kill+reap sigue en el
    ``finally`` de ``run_capture``, antes de que el runner eleve). Se verifica
    además que se preservan la causa original (``TimeoutError``) y el
    ``timeout_seconds`` configurado.
    """
    proc = _hanging_proc()
    runner, args = factory(tmp_path, 0.05)
    call = _CALLS[type(runner)]
    exc_type = _TIMEOUT_EXC[type(runner)]

    with patch("asyncio.create_subprocess_exec", return_value=proc), pytest.raises(exc_type) as exc_info:
        await call(runner, args)

    # Payload y cadena de causa preservados (raise ... from exc), no solo el tipo.
    assert exc_info.value.timeout_seconds == 0.05
    assert isinstance(exc_info.value.__cause__, TimeoutError)
    proc.kill.assert_called_once()
    proc.wait.assert_awaited()


@pytest.mark.parametrize("factory", _FACTORIES_QUE_SPAWNEAN)
async def test_runner_kills_process_on_cancel(tmp_path, factory, job_simulado):
    """On task cancellation (graceful shutdown) the runner must kill the child
    and re-raise ``CancelledError`` (never swallow cancellation).

    La cancelación se dispara cuando el spawn YA ocurrió, no tras un ``sleep``
    fijo. El invariante que este test protege es "si hay un hijo, se mata"; con
    un sleep arbitrario el test también dependía de que el runner llegara a
    spawnear dentro de esa ventana, y eso es una propiedad del RUNNER, no del
    invariante. Cualquier trabajo previo al spawn —BodySlide siembra su
    ``Config.xml`` para evitar los modales de arranque— corría la ventana y el
    test fallaba en un runner de CI cargado con "kill llamado 0 veces", que
    describe un escenario donde no había proceso que matar.
    """
    proc = _hanging_proc()
    runner, args = factory(tmp_path, 30.0)
    call = _CALLS[type(runner)]
    spawneado = asyncio.Event()

    async def _spawnear(*_args: object, **_kwargs: object):
        spawneado.set()
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=_spawnear):
        task = asyncio.create_task(call(runner, args))
        await asyncio.wait_for(spawneado.wait(), timeout=10.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    proc.kill.assert_called_once()
