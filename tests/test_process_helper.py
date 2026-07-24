"""Unit tests for the unified external-process helper (M-1).

`sky_claw/local/tools/_process.py` consolidates the subprocess dance that was
duplicated across every ``*_runner.py`` (exec -> communicate w/ timeout ->
kill+reap, plus CancelledError handling). The contract:

- ``kill_and_reap`` is None-safe, kills the process, and reaps it with a bounded
  wait that suppresses ONLY ``TimeoutError`` — a shutdown cancellation during the
  reap must propagate (matches ``windows_interop._kill_and_reap``).
- ``run_capture`` returns ``(stdout, stderr, returncode)`` on success; on timeout
  it kills+reaps and raises ``TimeoutError``; ``FileNotFoundError`` propagates;
  on cancellation it kills+reaps and re-raises ``CancelledError`` (no orphan).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sky_claw.local.tools._process import kill_and_reap, run_capture, spawn_detached


def _proc(*, communicate=None, returncode=0, wait_rc=-9) -> AsyncMock:
    proc = AsyncMock()
    proc.communicate = communicate or AsyncMock(return_value=(b"out", b"err"))
    proc.wait = AsyncMock(return_value=wait_rc)
    proc.kill = MagicMock()  # asyncio subprocess .kill() is synchronous
    proc.returncode = returncode
    return proc


# --- kill_and_reap ----------------------------------------------------------


async def test_kill_and_reap_none_is_noop():
    await kill_and_reap(None)  # must not raise


async def test_kill_and_reap_kills_and_reaps():
    proc = _proc()
    await kill_and_reap(proc)
    proc.kill.assert_called_once()
    proc.wait.assert_awaited()


async def test_kill_and_reap_suppresses_only_reap_timeout():
    async def _hang(*_a, **_k):
        await asyncio.sleep(3600)

    proc = _proc()
    proc.wait = AsyncMock(side_effect=_hang)
    # Bounded reap must not hang nor raise on timeout.
    await asyncio.wait_for(kill_and_reap(proc, timeout=0.05), timeout=2.0)
    proc.kill.assert_called_once()


async def test_kill_and_reap_propagates_cancellation():
    proc = _proc()
    proc.wait = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await kill_and_reap(proc)
    proc.kill.assert_called_once()  # killed before the cancel propagated


async def test_kill_and_reap_mata_arbol_en_windows(monkeypatch):
    """En Windows debe matar el ÁRBOL (taskkill /T), no solo el hijo directo.

    ``proc.kill()`` deja huérfanos a los nietos: DynDOLOD lanza TexGen, xEdit
    puede lanzar procesos auxiliares. ``taskkill /F /T /PID`` termina el árbol.
    """
    import sky_claw.local.tools._process as _process

    monkeypatch.setattr(_process.sys, "platform", "win32")
    recorded: list[list[str]] = []

    def _fake_run(args, **_kwargs):
        recorded.append(list(args))
        return MagicMock(returncode=0)

    monkeypatch.setattr(_process.subprocess, "run", _fake_run)

    proc = _proc()
    proc.pid = 4321  # PID real (int) → dispara el tree-kill
    await kill_and_reap(proc)

    assert recorded, "taskkill no fue invocado"
    args = recorded[0]
    assert "/T" in args and "4321" in args  # árbol completo por PID
    proc.kill.assert_called_once()  # el reap del hijo directo sigue ocurriendo
    proc.wait.assert_awaited()


async def test_kill_and_reap_taskkill_usa_timeout(monkeypatch):
    """taskkill se invoca con ``timeout`` acotado — si se cuelga (AV, PID raro)
    no debe bloquear indefinidamente el flujo de cleanup."""
    import sky_claw.local.tools._process as _process

    monkeypatch.setattr(_process.sys, "platform", "win32")
    captured: dict = {}

    def _fake_run(args, **kwargs):
        captured["kwargs"] = kwargs
        return MagicMock(returncode=0)

    monkeypatch.setattr(_process.subprocess, "run", _fake_run)

    proc = _proc()
    proc.pid = 4321
    await kill_and_reap(proc)

    assert captured["kwargs"].get("timeout") == _process._TASKKILL_TIMEOUT


async def test_kill_and_reap_no_usa_taskkill_fuera_de_windows(monkeypatch):
    """En POSIX (dev/CI) no hay taskkill; se conserva el comportamiento previo."""
    import sky_claw.local.tools._process as _process

    monkeypatch.setattr(_process.sys, "platform", "linux")
    called: list = []
    monkeypatch.setattr(_process.subprocess, "run", lambda *a, **k: called.append(a))

    proc = _proc()
    proc.pid = 4321
    await kill_and_reap(proc)

    assert not called
    proc.kill.assert_called_once()


async def test_kill_and_reap_taskkill_best_effort_no_propaga(monkeypatch):
    """Si taskkill falla (proceso ya muerto, binario ausente), no debe romper
    el reap — la garantía dura es proc.kill()+wait()."""
    import sky_claw.local.tools._process as _process

    monkeypatch.setattr(_process.sys, "platform", "win32")

    def _boom(*_a, **_k):
        raise OSError("taskkill no disponible")

    monkeypatch.setattr(_process.subprocess, "run", _boom)

    proc = _proc()
    proc.pid = 4321
    await kill_and_reap(proc)  # no debe propagar

    proc.kill.assert_called_once()
    proc.wait.assert_awaited()


# --- run_capture ------------------------------------------------------------


async def test_run_capture_returns_streams_and_code_on_success():
    proc = _proc(communicate=AsyncMock(return_value=(b"hello", b"")), returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        stdout, stderr, rc = await run_capture(["tool.exe", "--flag"], timeout=5.0)
    assert (stdout, stderr, rc) == (b"hello", b"", 0)


async def test_run_capture_kills_and_raises_on_timeout():
    async def _hang(*_a, **_k):
        await asyncio.sleep(3600)

    proc = _proc(communicate=AsyncMock(side_effect=_hang))
    with patch("asyncio.create_subprocess_exec", return_value=proc), pytest.raises(TimeoutError):
        await asyncio.wait_for(run_capture(["tool.exe"], timeout=0.05), timeout=2.0)
    proc.kill.assert_called_once()
    proc.wait.assert_awaited()  # killed AND reaped


async def test_run_capture_propagates_file_not_found():
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError), pytest.raises(FileNotFoundError):
        await run_capture(["missing.exe"], timeout=5.0)


async def test_run_capture_kills_on_unexpected_error():
    # A non-timeout/non-cancel failure after spawn (e.g. a pipe/I-O error) must
    # still kill+reap the child — otherwise runners that delegate cleanup here
    # would orphan the external tool.
    proc = _proc(communicate=AsyncMock(side_effect=OSError("pipe broke")))
    with patch("asyncio.create_subprocess_exec", return_value=proc), pytest.raises(OSError, match="pipe broke"):
        await run_capture(["tool.exe"], timeout=5.0)
    proc.kill.assert_called_once()


async def test_run_capture_returncode_none_no_se_enmascara_como_exito():
    """U-11: si ``communicate()`` vuelve OK pero ``returncode`` queda ``None``
    (no debería pasar en asyncio real — ``communicate()`` espera a que el
    proceso salga — pero el tipo lo permite), ``run_capture`` debe fallar
    fuerte en vez de mentir con ``return_code=0``: un caller que solo mira
    ``return_code == 0`` reportaría éxito sobre un estado indeterminado
    (el mismo patrón de falso verde que F5 marca para exit-codes).
    """
    proc = _proc(communicate=AsyncMock(return_value=(b"out", b"err")), returncode=None)
    with patch("asyncio.create_subprocess_exec", return_value=proc), pytest.raises(OSError, match="returncode"):
        await run_capture(["tool.exe"], timeout=5.0)
    # Estado indeterminado → se trata como salida no-normal: kill+reap igual.
    proc.kill.assert_called_once()
    proc.wait.assert_awaited()


async def test_run_capture_asigna_job_object_y_lo_cierra_en_exito():
    """U-02: ``run_capture`` es el helper compartido por xEdit, Wrye Bash, Pandora,
    BodySlide, Synthesis y la detección de versión de LOOT — cablear el Job Object
    ACÁ (en vez de por-runner) cubre los 6 de una, como ya hizo U-07 para DynDOLOD
    (que no pasa por ``run_capture``, tiene su propio spawn con drain de pipes).

    Sin Job Object, la muerte DURA del proceso Python (SIGKILL/OOM/corte de luz)
    orfana el árbol completo porque toda la garantía anti-huérfano vive en
    ``finally``/``kill_and_reap``, que una muerte dura no ejecuta (F1 de la
    auditoría). Meter el hijo en un job kill-on-close ANTES de esperarlo cierra
    ese hueco: si Python muere duro, Windows cierra sus handles —incluido el del
    job— y el propio SO mata el árbol. Cerrar el job explícitamente en éxito
    también aniquila cualquier nieto que sobreviva al padre (mismo patrón que
    U-07/F3 para DynDOLOD)."""
    import sky_claw.local.tools._process as _process

    proc = _proc(communicate=AsyncMock(return_value=(b"out", b"")), returncode=0)
    proc.pid = 4242
    assign_spy = MagicMock(return_value=99)
    close_spy = MagicMock()
    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch.object(_process, "assign_kill_on_close_job", assign_spy),
        patch.object(_process, "close_job", close_spy),
    ):
        await run_capture(["tool.exe"], timeout=5.0)

    assign_spy.assert_called_once_with(4242)
    close_spy.assert_called_once_with(99)


async def test_run_capture_cierra_el_job_en_timeout():
    """El job debe cerrarse también en el path de timeout — no solo en éxito —
    para no filtrar el handle y para que el cierre aniquile cualquier nieto que
    ``kill_and_reap`` no alcance."""
    import sky_claw.local.tools._process as _process

    async def _hang(*_a, **_k):
        await asyncio.sleep(3600)

    proc = _proc(communicate=AsyncMock(side_effect=_hang))
    proc.pid = 4242
    close_spy = MagicMock()
    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch.object(_process, "assign_kill_on_close_job", MagicMock(return_value=99)),
        patch.object(_process, "close_job", close_spy),
        pytest.raises(TimeoutError),
    ):
        await asyncio.wait_for(run_capture(["tool.exe"], timeout=0.05), timeout=2.0)

    close_spy.assert_called_once_with(99)


async def test_run_capture_cierra_el_job_aunque_kill_and_reap_falle():
    """CodeRabbit (PR #360): si ``kill_and_reap(proc)`` lanza (o es cancelado)
    durante el cleanup de una salida no-normal, ``close_job(job)`` NO debe
    omitirse — si no, el job queda abierto y el árbol que debía aniquilar sigue
    vivo, contradiciendo la garantía de "cerrar el job en TODA salida"."""
    import sky_claw.local.tools._process as _process

    async def _hang(*_a, **_k):
        await asyncio.sleep(3600)

    proc = _proc(communicate=AsyncMock(side_effect=_hang))
    proc.pid = 4242
    close_spy = MagicMock()
    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch.object(_process, "assign_kill_on_close_job", MagicMock(return_value=99)),
        patch.object(_process, "close_job", close_spy),
        patch.object(_process, "kill_and_reap", AsyncMock(side_effect=OSError("reap boom"))),
        pytest.raises(OSError, match="reap boom"),
    ):
        await asyncio.wait_for(run_capture(["tool.exe"], timeout=0.05), timeout=2.0)

    close_spy.assert_called_once_with(99)


async def test_run_capture_kills_and_reraises_on_cancel():
    async def _hang(*_a, **_k):
        await asyncio.sleep(3600)

    proc = _proc(communicate=AsyncMock(side_effect=_hang))
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        task = asyncio.create_task(run_capture(["tool.exe"], timeout=30.0))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    proc.kill.assert_called_once()


# --- spawn_detached ---------------------------------------------------------


async def test_spawn_detached_returns_process_fire_and_forget():
    """Lanzamiento interactivo: devuelve el proceso SIN capturar salida ni
    matar/reap — la GUI la opera y cierra el usuario (p. ej. xEdit para forwardeo
    manual). Sin PIPE no se bloquea con pipes llenos en una sesión larga."""
    proc = _proc()
    with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        returned = await spawn_detached(["xEdit.exe", "-SSE", "P.esp"])

    assert returned is proc
    _, kwargs = spawn.call_args
    # Sin PIPE: no capturamos stdout/stderr de una sesión interactiva larga.
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs
    # El proceso debe SOBREVIVIR a la llamada: nunca se mata/reap acá.
    proc.kill.assert_not_called()


async def test_spawn_detached_windows_suppresses_console(monkeypatch):
    """En Windows aplica CREATE_NO_WINDOW: la GUI aparece igual, solo se suprime
    la consola de la que colgaría el editor."""
    import sky_claw.local.tools._process as _process

    monkeypatch.setattr(_process.sys, "platform", "win32")
    proc = _proc()
    with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
        await spawn_detached(["xEdit.exe"])

    _, kwargs = spawn.call_args
    assert kwargs.get("creationflags") == _process._CREATE_NO_WINDOW
