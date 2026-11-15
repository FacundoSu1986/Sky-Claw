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
import sys
from pathlib import Path
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


async def test_run_capture_input_bytes_llega_por_stdin_a_un_proceso_real():
    # El cliente MCP del xEdit Bridge habla JSON-RPC por stdin: el helper unificado tiene que poder enviarlo.
    import sys

    codigo = "import sys; sys.stdout.write(sys.stdin.read().upper())"
    stdout, _stderr, rc = await run_capture([sys.executable, "-c", codigo], timeout=30.0, input_bytes=b"hola bridge")
    assert (stdout, rc) == (b"HOLA BRIDGE", 0)


async def test_run_capture_sin_input_bytes_no_abre_stdin_ni_lo_envia():
    proc = _proc(communicate=AsyncMock(return_value=(b"x", b"")), returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=proc) as crear:
        await run_capture(["tool.exe"], timeout=5.0)
    assert "stdin" not in crear.call_args.kwargs, "sin input_bytes el comportamiento previo no cambia"
    proc.communicate.assert_awaited_once_with()


async def test_run_capture_con_input_bytes_abre_pipe_y_lo_envia():
    proc = _proc(communicate=AsyncMock(return_value=(b"x", b"")), returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=proc) as crear:
        await run_capture(["tool.exe"], timeout=5.0, input_bytes=b"{}\n")
    assert crear.call_args.kwargs["stdin"] is asyncio.subprocess.PIPE
    proc.communicate.assert_awaited_once_with(input=b"{}\n")


async def test_run_capture_env_llega_al_proceso_real():
    import os
    import sys

    codigo = "import os, sys; sys.stdout.write(os.environ.get('SKYCLAW_PRUEBA_ENV', '-'))"
    entorno = {**os.environ, "SKYCLAW_PRUEBA_ENV": "desde el caller"}
    stdout, _stderr, rc = await run_capture([sys.executable, "-c", codigo], timeout=30.0, env=entorno)
    assert (stdout, rc) == (b"desde el caller", 0)


async def test_run_capture_sin_env_no_lo_pasa_y_el_hijo_hereda():
    proc = _proc(communicate=AsyncMock(return_value=(b"x", b"")), returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=proc) as crear:
        await run_capture(["tool.exe"], timeout=5.0)
    assert "env" not in crear.call_args.kwargs, "sin env el comportamiento previo no cambia"


async def test_run_capture_con_input_bytes_y_timeout_mata_y_cosecha():
    async def _colgado(*_a, **_k):
        await asyncio.sleep(3600)

    proc = _proc(communicate=AsyncMock(side_effect=_colgado))
    with patch("asyncio.create_subprocess_exec", return_value=proc), pytest.raises(TimeoutError):
        await asyncio.wait_for(run_capture(["tool.exe"], timeout=0.05, input_bytes=b"{}\n"), timeout=2.0)
    proc.kill.assert_called_once()
    proc.wait.assert_awaited()


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


# --- assign_kill_on_close_job ------------------------------------------------


def test_assign_kill_on_close_job_retorna_none_sin_lanzar_con_pids_invalidos():
    """Contrato best-effort: PIDs inválidos (None, <=0, tipos no-int) retornan None
    sin tocar Win32 y sin lanzar excepción."""
    import sky_claw.local.tools._process as _process

    for pid_invalido in (None, 0, -1, -999, "1234", 3.14):
        assert _process.assign_kill_on_close_job(pid_invalido) is None  # type: ignore[arg-type]


def test_assign_kill_on_close_job_captura_fallos_de_win32_sin_lanzar(monkeypatch):
    """Contrato best-effort: ante cualquier fallo de ctypes / Win32 (OpenProcess fallido,
    ArgumentError, OSError), assign_kill_on_close_job devuelve None sin propagar excepción."""
    import ctypes
    from unittest.mock import MagicMock

    import sky_claw.local.tools._process as _process

    monkeypatch.setattr(_process.sys, "platform", "win32")

    # Caso 1: CreateJobObjectW falla (retorna 0)
    fake_k32 = MagicMock()
    fake_k32.CreateJobObjectW.return_value = 0
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: fake_k32)
    assert _process.assign_kill_on_close_job(1234) is None

    # Caso 2: ctypes lanza ArgumentError
    def _raise_arg_error(*_a, **_k):
        raise ctypes.ArgumentError("fake ctypes argument error")

    fake_k32.CreateJobObjectW.side_effect = _raise_arg_error
    assert _process.assign_kill_on_close_job(1234) is None

    # Caso 3: OpenProcess falla con OSError
    def _raise_os_error(*_a, **_k):
        raise OSError("fake OS error")

    fake_k32.CreateJobObjectW.side_effect = _raise_os_error
    assert _process.assign_kill_on_close_job(1234) is None


def test_dyndolod_process_assign_job_contrato_seguro():
    """Contrato de assign_job para DynDOLODProcess en standalone y brokered:
    ambos son best-effort y devuelven None o int sin propagar excepciones."""
    from unittest.mock import MagicMock

    from sky_claw.local.mo2.brokered_dyndolod import BrokeredDynDOLODProcess
    from sky_claw.local.tools.dyndolod_runner import _StandaloneDynDOLODProcess

    # Brokered: siempre None por diseño
    sesion_fake = MagicMock()
    brokered = BrokeredDynDOLODProcess(sesion_fake)
    assert brokered.assign_job() is None

    # Standalone con proceso mock inválido/inactivo: devuelve None sin lanzar
    proc_fake = MagicMock()
    proc_fake.pid = -1
    standalone = _StandaloneDynDOLODProcess(proc_fake)
    assert standalone.assign_job() is None


# --- XB-SC-04 & XB-SC-05: require_job y captura acotada (max_output_bytes) -----------------


def _pid_vivo_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    if sys.platform != "win32" or pid <= 0:
        return False
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        err = ctypes.get_last_error()
        if err == 87:  # ERROR_INVALID_PARAMETER: PID inexistente
            return False
        raise OSError(err, ctypes.FormatError(err))
    try:
        codigo = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(codigo)):
            err = ctypes.get_last_error()
            raise OSError(err, ctypes.FormatError(err))
        return codigo.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def test_pid_vivo_windows_reporta_proceso_actual_y_muerto():
    """Control positivo/negativo para ``_pid_vivo_windows``: distingue proceso vivo de ya terminado."""
    if sys.platform != "win32":
        pytest.skip("específico de Windows")
    import os
    import subprocess

    assert _pid_vivo_windows(os.getpid()) is True
    assert _pid_vivo_windows(0) is False
    hijo = subprocess.Popen([sys.executable, "-c", "pass"])
    pid_hijo = hijo.pid
    hijo.wait(timeout=10)
    assert _pid_vivo_windows(pid_hijo) is False


async def test_run_capture_require_job_true_mata_y_falla_cerrado_si_job_es_none(monkeypatch):
    import sky_claw.local.tools._process as _process

    monkeypatch.setattr(_process.sys, "platform", "win32")
    proc = _proc(communicate=AsyncMock(return_value=(b"ok", b"")), returncode=0)
    proc.pid = 4242
    close_spy = MagicMock()
    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch.object(_process, "assign_kill_on_close_job", MagicMock(return_value=None)),
        patch.object(_process, "close_job", close_spy),
        pytest.raises(OSError, match="Job Object"),
    ):
        await run_capture(["tool.exe"], timeout=5.0, require_job=True)

    proc.kill.assert_called_once()
    proc.wait.assert_awaited()
    close_spy.assert_called_once_with(None)


async def test_run_capture_require_job_true_no_falla_fuera_de_windows(monkeypatch):
    import sky_claw.local.tools._process as _process

    monkeypatch.setattr(_process.sys, "platform", "linux")
    proc = _proc(communicate=AsyncMock(return_value=(b"ok", b"")), returncode=0)
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        stdout, stderr, rc = await run_capture(["tool"], timeout=5.0, require_job=True)
    assert (stdout, stderr, rc) == (b"ok", b"", 0)


@pytest.mark.parametrize("bad_limit", [0, -1, -4096, True, False])
async def test_run_capture_rechaza_max_output_bytes_invalido(bad_limit):
    with patch("asyncio.create_subprocess_exec") as spawner, pytest.raises(ValueError, match="max_output_bytes"):
        await run_capture(["tool.exe"], timeout=5.0, max_output_bytes=bad_limit)
    spawner.assert_not_called()


@pytest.mark.parametrize("canal", ["stdout", "stderr", "combinado"])
async def test_run_capture_max_output_bytes_mata_proceso_real_sin_retornar_json_truncado(tmp_path: Path, canal: str):
    """XB-SC-05: ante exceso en stdout, stderr o la suma de ambos, mata+reap el hijo real
    y eleva OSError sin entregar salida parcial."""
    pid_file = tmp_path / "child.pid"
    if canal == "stdout":
        emisor = (
            "sys.stdout.buffer.write(b'{\"ok\": true'); sys.stdout.buffer.flush();"
            "sys.stdout.buffer.write(b'X' * 131072); sys.stdout.buffer.flush();"
        )
    elif canal == "stderr":
        emisor = "sys.stderr.buffer.write(b'E' * 131072); sys.stderr.buffer.flush();"
    else:
        emisor = (
            "sys.stdout.buffer.write(b'A' * 600); sys.stdout.buffer.flush();"
            "sys.stderr.buffer.write(b'B' * 600); sys.stderr.buffer.flush();"
        )
    codigo = (
        f"import os, sys, time; from pathlib import Path; "
        f"Path({repr(str(pid_file))}).write_text(str(os.getpid()), encoding='utf-8'); "
        f"{emisor} time.sleep(60)"
    )

    with pytest.raises(OSError, match="límite"):
        await asyncio.wait_for(
            run_capture(
                [sys.executable, "-c", codigo],
                timeout=15.0,
                max_output_bytes=1024,
                require_job=True,
            ),
            timeout=10.0,
        )

    assert pid_file.exists()
    pid = int(pid_file.read_text(encoding="utf-8"))
    if sys.platform == "win32":
        assert not _pid_vivo_windows(pid)


async def test_run_capture_acotado_con_stdin_y_salida_valida_sobre_proceso_real():
    """Con ``input_bytes`` + ``max_output_bytes`` dentro de rango, conserva entrada/salida intactas."""
    codigo = (
        "import sys; data = sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(data.upper()); sys.stderr.buffer.write(b'ok')"
    )
    stdout, stderr, rc = await run_capture(
        [sys.executable, "-c", codigo],
        timeout=15.0,
        input_bytes=b'{"id": 1}\n',
        max_output_bytes=4096,
        require_job=True,
    )
    assert (stdout, stderr, rc) == (b'{"ID": 1}\n', b"ok", 0)


async def test_run_capture_require_job_termina_nieto_huerfano_en_windows(tmp_path: Path):
    """XB-SC-04: en Windows, al terminar por límite o cancelación un hijo que lanzó un nieto
    sin heredar pipes, ``close_job`` + ``kill_and_reap`` matan también al nieto."""
    if sys.platform != "win32":
        pytest.skip("Job Objects solo existen en Windows")
    nieto_pid_file = tmp_path / "grandchild.pid"
    codigo = (
        "import subprocess, sys, time; from pathlib import Path; "
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        "creationflags=0x08000000); "
        f"Path({repr(str(nieto_pid_file))}).write_text(str(p.pid), encoding='utf-8'); "
        "sys.stdout.write('READY\\n'); sys.stdout.flush(); time.sleep(60)"
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            run_capture(
                [sys.executable, "-c", codigo],
                timeout=1.2,
                max_output_bytes=4096,
                require_job=True,
            ),
            timeout=10.0,
        )

    assert nieto_pid_file.exists()
    nieto_pid = int(nieto_pid_file.read_text(encoding="utf-8"))
    # Esperar brevemente a que el SO termine de desmontar el Job Object.
    for _ in range(20):
        if not _pid_vivo_windows(nieto_pid):
            break
        await asyncio.sleep(0.05)
    assert not _pid_vivo_windows(nieto_pid), f"el nieto {nieto_pid} quedó vivo tras cerrar el Job"


async def test_run_capture_require_job_termina_nieto_en_salida_normal_y_cancelacion(tmp_path: Path):
    """XB-SC-04: incluso ante salida normal (rc=0) o cancelación, ``close_job`` y ``kill_and_reap``
    aniquilan descendientes reparentados en Windows."""
    if sys.platform != "win32":
        pytest.skip("Job Objects solo existen en Windows")
    nieto_normal = tmp_path / "nieto_normal.pid"
    codigo_normal = (
        "import subprocess, sys; from pathlib import Path; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        "creationflags=0x08000000); "
        f"Path({repr(str(nieto_normal))}).write_text(str(p.pid), encoding='utf-8'); "
        "sys.stdout.write('OK'); sys.stdout.flush()"
    )
    out, _err, rc = await run_capture(
        [sys.executable, "-c", codigo_normal],
        timeout=15.0,
        max_output_bytes=4096,
        require_job=True,
    )
    assert (out, rc) == (b"OK", 0)
    pid_n = int(nieto_normal.read_text(encoding="utf-8"))
    for _ in range(20):
        if not _pid_vivo_windows(pid_n):
            break
        await asyncio.sleep(0.05)
    assert not _pid_vivo_windows(pid_n), f"nieto {pid_n} sobrevivió a salida normal del padre"

    nieto_cancel = tmp_path / "nieto_cancel.pid"
    codigo_cancel = (
        "import subprocess, sys, time; from pathlib import Path; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        "creationflags=0x08000000); "
        f"Path({repr(str(nieto_cancel))}).write_text(str(p.pid), encoding='utf-8'); "
        "time.sleep(60)"
    )
    tarea = asyncio.create_task(
        run_capture(
            [sys.executable, "-c", codigo_cancel],
            timeout=30.0,
            max_output_bytes=4096,
            require_job=True,
        )
    )
    for _ in range(80):
        if nieto_cancel.exists() and nieto_cancel.read_text(encoding="utf-8").strip().isdigit():
            break
        await asyncio.sleep(0.05)
    assert nieto_cancel.exists()
    pid_c = int(nieto_cancel.read_text(encoding="utf-8"))
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    for _ in range(20):
        if not _pid_vivo_windows(pid_c):
            break
        await asyncio.sleep(0.05)
    assert not _pid_vivo_windows(pid_c), f"nieto {pid_c} sobrevivió a cancelación"


async def test_run_capture_acotado_tolera_hijo_que_sale_sin_leer_stdin():
    """Si el hijo sale rápido sin leer stdin (``BrokenPipeError``), ``run_capture`` reporta su
    salida y returncode reales sin romperse en ``_escribir_stdin``."""
    codigo = "import sys; sys.stdout.write('rapido'); sys.exit(2)"
    out, _err, rc = await run_capture(
        [sys.executable, "-c", codigo],
        timeout=15.0,
        input_bytes=b"X" * 131072,
        max_output_bytes=4096,
    )
    assert (out, rc) == (b"rapido", 2)
