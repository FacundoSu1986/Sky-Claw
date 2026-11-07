"""Preflight de breakaway de MO2 contra Job Objects REALES: matriz de ``LimitFlags`` del núcleo.

Las pruebas de ``tests/test_mo2_bridge_plugin.py`` fijan la DECISIÓN del preflight
inyectando ``query_flags`` o un ``kernel32`` falso; no prueban la semántica del sistema
operativo. Acá el Job lo crea el núcleo con flags exactos, un proceso Python real queda
asignado a él y se comprueba, contra el kernel de Windows:

* ``_query_current_process_job_limit_flags()`` ve exactamente los ``LimitFlags`` del Job;
* ``verify_mo2_breakaway_allowed()`` acepta/rechaza según la matriz de flags;
* ``CreateProcessW(CREATE_BREAKAWAY_FROM_JOB)`` real tiene éxito exactamente cuando el Job
  inmediato concede breakaway (``BREAKAWAY_OK`` o ``SILENT_BREAKAWAY_OK``), que es la
  premisa sobre la que el preflight falla cerrado;
* con Jobs ANIDADOS el kernel mira el Job inmediato (el último asignado): un Job exterior
  con permiso no rescata a un Job interior sin permiso, y el preflight coincide.

No usa MO2, USVFS ni el sandbox: los únicos procesos que lanza son ``python.exe`` inocuos
(``-c`` que termina solo) y Jobs anónimos descartables. Fuera de Windows se omite con motivo
explícito — un ``SKIPPED`` no es un ``PASS``: la evidencia vive en la corrida Windows de CI.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Job Objects reales: solo Windows (la evidencia vive en la corrida Windows de CI)",
)

REPO = pathlib.Path(__file__).resolve().parents[1]

#: En un venv de Windows ``sys.executable`` puede ser ``venvlauncher.exe``: un trampoline que
#: crea su propio Job e inicia el ``python.exe`` real como hijo ANTES de que podamos asignar.
#: Para medir el kernel sobre un proceso directo usamos ``sys._base_executable``.
PYTHON_DIRECTO = getattr(sys, "_base_executable", sys.executable)

JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
ERROR_ACCESS_DENIED = 5

#: Matriz del contrato del preflight: acepta solo si el Job inmediato tiene BREAKAWAY_OK o
#: SILENT_BREAKAWAY_OK. Incluye los flags que MO2/USVFS usa hoy (KILL_ON_JOB_CLOSE) para
#: probar que no cambian la decisión.
_CASOS = [
    pytest.param(0x0000, False, id="sin_flags"),
    pytest.param(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, False, id="solo_kill_on_job_close"),
    pytest.param(JOB_OBJECT_LIMIT_BREAKAWAY_OK, True, id="solo_breakaway_ok"),
    pytest.param(JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK, True, id="solo_silent_breakaway_ok"),
    pytest.param(
        JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
        True,
        id="breakaway_ok_y_silent",
    ),
    pytest.param(
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK,
        True,
        id="kill_y_breakaway_ok",
    ),
    pytest.param(
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
        True,
        id="kill_y_silent_breakaway_ok",
    ),
    pytest.param(
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
        True,
        id="kill_breakaway_ok_y_silent",
    ),
]

#: Casos anidados (exterior, interior, el preflight debe aceptar): con Jobs anidados el
#: kernel evalúa el Job INMEDIATO (el último asignado), no el exterior.
_CASOS_ANIDADOS = [
    pytest.param(
        JOB_OBJECT_LIMIT_BREAKAWAY_OK,
        0x0000,
        False,
        id="exterior_con_permiso_interior_sin_permiso",
    ),
    pytest.param(
        0x0000,
        JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
        True,
        id="exterior_sin_permiso_interior_con_permiso",
    ),
]

_CODIGO_HIJO = textwrap.dedent(
    """
    import json, os, subprocess, sys, time

    from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge import runtime

    listo, seguir, salida = sys.argv[1], sys.argv[2], sys.argv[3]
    open(listo, "w").write(str(os.getpid()))
    while not os.path.exists(seguir):
        time.sleep(0.005)

    informe = {
        "pid": os.getpid(),
        "flags_vistos": runtime._query_current_process_job_limit_flags(),
    }
    try:
        runtime.verify_mo2_breakaway_allowed()
        informe["preflight"] = "acepta"
        informe["preflight_error"] = None
    except Exception as exc:
        informe["preflight"] = "rechaza"
        informe["preflight_error"] = str(exc)
    try:
        nieto = subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(0)"],
            creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        informe["kernel_breakaway"] = True
        informe["kernel_rc"] = nieto.wait(timeout=30)
        informe["kernel_winerror"] = None
    except OSError as exc:
        informe["kernel_breakaway"] = False
        informe["kernel_rc"] = None
        informe["kernel_winerror"] = getattr(exc, "winerror", None)
        informe["kernel_error"] = str(exc)
    open(salida, "w").write(json.dumps(informe))
    """
)


def _sondas():
    """Import diferido: el módulo del laboratorio solo es utilizable en Windows."""
    from scripts import windows_job_probe

    return windows_job_probe


def _entorno_python() -> dict[str, str]:
    rutas = [str(REPO), *(p for p in sys.path if p)]
    return {**os.environ, "PYTHONPATH": os.pathsep.join(rutas)}


def _esperar_archivo(ruta: pathlib.Path, *, timeout: float = 60.0) -> None:
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if ruta.is_file() and ruta.read_text(encoding="utf-8").strip():
            return
        time.sleep(0.005)
    raise AssertionError(f"el hijo sonda no creó {ruta.name} en {timeout}s")


def _asignar_al_job(sondas, job: int, pid: int) -> None:
    handle = sondas.abrir_proceso(pid)
    try:
        sondas.asignar_a_job(job, handle)
    finally:
        sondas.cerrar_handle(handle)


def _detener(proceso: subprocess.Popen[bytes]) -> None:
    if proceso.poll() is None:
        proceso.kill()
        proceso.communicate(timeout=30)


def _lanzar_sonda(sondas, tmp_path: pathlib.Path) -> subprocess.Popen[bytes]:
    listo = tmp_path / "listo.txt"
    seguir = tmp_path / "seguir.txt"
    salida = tmp_path / "informe.json"
    proceso = subprocess.Popen(  # noqa: S603 - argv fijo del laboratorio, procesos inocuos
        [PYTHON_DIRECTO, "-c", _CODIGO_HIJO, str(listo), str(seguir), str(salida)],
        cwd=REPO,
        env=_entorno_python(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _esperar_archivo(listo)
    except Exception:
        _detener(proceso)
        raise
    return proceso


def _recolectar(proceso: subprocess.Popen[bytes], tmp_path: pathlib.Path) -> dict[str, object]:
    stderr = b""
    try:
        (tmp_path / "seguir.txt").write_text("seguir", encoding="utf-8")
        _, stderr = proceso.communicate(timeout=90)
    finally:
        _detener(proceso)
    assert proceso.returncode == 0, f"el hijo sonda falló ({proceso.returncode}): {stderr.decode(errors='replace')}"
    return json.loads((tmp_path / "informe.json").read_text(encoding="utf-8"))


def _verificar_informe(
    sondas,
    *,
    job: int,
    limite_flags_esperados: int,
    permite: bool,
    informe: dict[str, object],
) -> None:
    assert sondas.limit_flags_del_job(job) == limite_flags_esperados, (
        "el núcleo no reportó los LimitFlags que pidió el test"
    )
    assert informe["flags_vistos"] == limite_flags_esperados, (
        f"el proceso asignado no ve el Job inmediato: {informe['flags_vistos']!r}"
    )
    if permite:
        assert informe["preflight"] == "acepta", informe.get("preflight_error")
    else:
        assert informe["preflight"] == "rechaza", "el preflight aceptó un Job que el kernel rechaza"
        error = str(informe.get("preflight_error") or "")
        assert f"LimitFlags={limite_flags_esperados:#x}" in error, error
        assert "breakaway" in error, error
    assert informe["kernel_breakaway"] is permite, f"el kernel real no coincide con el preflight: {informe!r}"
    if not permite:
        assert informe["kernel_winerror"] == ERROR_ACCESS_DENIED, informe


@pytest.mark.parametrize(("limit_flags", "permite"), _CASOS)
def test_preflight_breakaway_contra_job_real_por_combinacion_de_flags(
    tmp_path: pathlib.Path,
    limit_flags: int,
    permite: bool,
) -> None:
    """El preflight coincide con el kernel real que crea y restringe el Job."""
    sondas = _sondas()
    job = sondas.crear_job_con_limit_flags(limit_flags)
    try:
        proceso = _lanzar_sonda(sondas, tmp_path)
        try:
            _asignar_al_job(sondas, job, proceso.pid)
            informe = _recolectar(proceso, tmp_path)
        finally:
            _detener(proceso)
        _verificar_informe(
            sondas,
            job=job,
            limite_flags_esperados=limit_flags,
            permite=permite,
            informe=informe,
        )
    finally:
        sondas.cerrar_handle(job)


@pytest.mark.parametrize(("flags_exterior", "flags_interior", "permite"), _CASOS_ANIDADOS)
def test_con_jobs_anidados_el_preflight_mira_el_job_inmediato_y_coincide_con_el_kernel(
    tmp_path: pathlib.Path,
    flags_exterior: int,
    flags_interior: int,
    permite: bool,
) -> None:
    """Jobs anidados: el permiso del Job interior es el que decide, como en el kernel."""
    sondas = _sondas()
    job_exterior = sondas.crear_job_con_limit_flags(flags_exterior)
    try:
        job_interior = sondas.crear_job_con_limit_flags(flags_interior)
    except Exception:
        sondas.cerrar_handle(job_exterior)
        raise
    try:
        proceso = _lanzar_sonda(sondas, tmp_path)
        try:
            _asignar_al_job(sondas, job_exterior, proceso.pid)
            _asignar_al_job(sondas, job_interior, proceso.pid)
            informe = _recolectar(proceso, tmp_path)
        finally:
            _detener(proceso)
        _verificar_informe(
            sondas,
            job=job_interior,
            limite_flags_esperados=flags_interior,
            permite=permite,
            informe=informe,
        )
    finally:
        sondas.cerrar_handle(job_interior)
        sondas.cerrar_handle(job_exterior)
