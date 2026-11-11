"""H-01 (P1) contra el kernel REAL de Windows: la adopción de descendientes y la cuenta de procesos del Job.

``test_mo2_bridge_adopcion.py`` prueba la lógica con una tabla de procesos en memoria. Acá se prueba la capa
``ctypes`` de verdad (ToolHelp32, ``OpenProcess``, ``IsProcessInJob`` contra el Job propio, ``AssignProcessToJobObject``,
``QueryInformationJobObject``) con procesos inocuos (``python.exe`` durmiendo): sin MO2, sin USVFS, sin herramientas.

Lo que fija:

* un hijo (y un nieto) creados ANTES de la asignación quedan FUERA del Job y la adopción los incluye;
* un hijo creado DESPUÉS de la asignación ya hereda el Job: la adopción no lo cuenta dos veces;
* ``active_processes`` sigue lo que realmente vive en el Job y llega a 0 tras terminarlo;
* con el Job real, un proceso AJENO que no desciende del lanzado nunca se toca.

Se omite fuera de Windows con motivo explícito: un skip NO cuenta como PASS; la evidencia vive en el sandbox Windows de CI.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import textwrap
import time

import psutil
import pytest

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge import runtime as puente
from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import (
    BridgeCommandError,
    Win32JobObject,
    adopt_descendants,
)
from sky_claw.local.mo2.vfs_job_identity import generar_nombre_de_job
from tests.test_vfs_job_containment_windows import (
    _PADRE_QUE_ESPERA_LA_BANDERA,
    _PADRE_QUE_YA_TIENE_HIJO,
    PYTHON_DIRECTO,
    REPO,
    _entorno_python,
    _esperar_muerte,
    _esperar_pid,
    _sondas,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Job Objects reales: solo Windows (la evidencia vive en el sandbox Windows de CI)",
)

#: Padre -> hijo -> nieto, todos nacidos antes de que el bridge asigne al padre.
_PADRE_CON_HIJO_Y_NIETO = textwrap.dedent(
    """
    import subprocess, sys, time
    ejecutable = getattr(sys, "_base_executable", sys.executable)
    codigo_del_hijo = (
        "import subprocess, sys, time\\n"
        "ejecutable = getattr(sys, '_base_executable', sys.executable)\\n"
        "nieto = subprocess.Popen([ejecutable, '-c', 'import time; time.sleep(120)'])\\n"
        "open(sys.argv[1], 'w').write(str(nieto.pid))\\n"
        "time.sleep(120)\\n"
    )
    hijo = subprocess.Popen([ejecutable, "-c", codigo_del_hijo, sys.argv[2]])
    open(sys.argv[1], "w").write(str(hijo.pid))
    time.sleep(120)
    """
)


@pytest.fixture
def limpieza():
    """Mata y REAPEA todo proceso que el test registre (``Popen``: kill + wait; o un PID suelto).

    Sin ``wait()`` el ``Popen`` queda con ``returncode=None`` y su ``__del__`` emite un ``ResourceWarning`` que este
    repo eleva a error. Es el mismo fixture de ``test_vfs_job_containment_windows``, local para no importar fixtures.
    """
    registrados: list[subprocess.Popen[bytes] | int] = []
    yield registrados
    sondas = _sondas()
    for item in registrados:
        pid = item.pid if isinstance(item, subprocess.Popen) else item
        sondas.matar_arbol(pid)
        if isinstance(item, subprocess.Popen):
            with contextlib.suppress(Exception):
                item.wait(timeout=10)


@pytest.fixture
def job():
    """Un ``Win32JobObject`` de producción con nombre nuevo (como el broker): se cierra siempre."""
    creado = Win32JobObject(generar_nombre_de_job())
    yield creado
    creado.close()


def _lanzar(argv_codigo: list[str]) -> subprocess.Popen[bytes]:
    return _sondas().lanzar_como_mo2([PYTHON_DIRECTO, *argv_codigo], cwd=REPO, env=_entorno_python())


def _esperar_activos(job: Win32JobObject, esperados: int, *, timeout: float = 10.0) -> int:
    limite = time.monotonic() + timeout
    activos = job.active_processes()
    while activos != esperados and time.monotonic() < limite:
        time.sleep(0.05)
        activos = job.active_processes()
    return activos


def test_un_job_recien_creado_no_tiene_procesos_activos(job) -> None:
    assert job.active_processes() == 0


def test_la_adopcion_real_incluye_al_hijo_previo_y_muere_con_el_job(tmp_path, limpieza, job) -> None:
    # Arrange: el "worker" ya creó a su hijo cuando el bridge lo asigna (la carrera del onefile).
    salida = tmp_path / "hijo.pid"
    padre = _lanzar(["-c", _PADRE_QUE_YA_TIENE_HIJO, str(salida)])
    limpieza.append(padre)
    hijo_pid = _esperar_pid(salida)
    limpieza.append(hijo_pid)
    handle_del_padre = _sondas().abrir_proceso(padre.pid)
    try:
        job.assign(handle_del_padre)
        antes = job.active_processes()  # solo el padre: el hijo previo NO fue adoptado por el assign

        # Act
        adoptados = job.adopt_descendants(handle_del_padre)
        despues = job.active_processes()
        job.terminate()

        # Assert
        assert antes == 1
        assert adoptados == 1
        assert despues == 2
        assert _esperar_muerte(padre.pid)
        assert _esperar_muerte(hijo_pid), "el hijo adoptado debía morir con el Job"
        assert _esperar_activos(job, 0) == 0
    finally:
        _sondas().cerrar_handle(handle_del_padre)


def test_la_adopcion_real_incluye_tambien_al_nieto(tmp_path, limpieza, job) -> None:
    # Arrange
    pid_hijo, pid_nieto = tmp_path / "hijo.pid", tmp_path / "nieto.pid"
    padre = _lanzar(["-c", _PADRE_CON_HIJO_Y_NIETO, str(pid_hijo), str(pid_nieto)])
    limpieza.append(padre)
    hijo_pid = _esperar_pid(pid_hijo)
    nieto_pid = _esperar_pid(pid_nieto)
    limpieza.extend([hijo_pid, nieto_pid])
    handle_del_padre = _sondas().abrir_proceso(padre.pid)
    try:
        job.assign(handle_del_padre)

        # Act
        adoptados = job.adopt_descendants(handle_del_padre)
        activos = job.active_processes()
        job.terminate()

        # Assert: el punto fijo llega hasta el nieto; todo el árbol muere con el Job.
        assert adoptados == 2
        assert activos == 3
        for pid in (padre.pid, hijo_pid, nieto_pid):
            assert _esperar_muerte(pid), f"el proceso {pid} debía morir con el Job"
    finally:
        _sondas().cerrar_handle(handle_del_padre)


def test_un_hijo_creado_despues_de_la_asignacion_hereda_el_job_y_no_se_adopta_dos_veces(
    tmp_path, limpieza, job
) -> None:
    # Arrange: el padre espera la bandera, así que su hijo nace DESPUÉS de la asignación.
    bandera, salida = tmp_path / "go", tmp_path / "hijo.pid"
    padre = _lanzar(["-c", _PADRE_QUE_ESPERA_LA_BANDERA, str(bandera), str(salida)])
    limpieza.append(padre)
    handle_del_padre = _sondas().abrir_proceso(padre.pid)
    try:
        job.assign(handle_del_padre)
        bandera.write_text("go", encoding="utf-8")
        hijo_pid = _esperar_pid(salida)
        limpieza.append(hijo_pid)

        # Act
        adoptados = job.adopt_descendants(handle_del_padre)
        activos = job.active_processes()
        job.terminate()

        # Assert: el hijo ya estaba en el Job por herencia.
        assert adoptados == 0
        assert activos == 2
        assert _esperar_muerte(padre.pid)
        assert _esperar_muerte(hijo_pid)
    finally:
        _sondas().cerrar_handle(handle_del_padre)


def test_la_adopcion_real_no_toca_a_un_proceso_ajeno_al_arbol(tmp_path, limpieza, job) -> None:
    # Arrange: un proceso que NO desciende del lanzado, vivo mientras se adopta.
    ajeno = _lanzar(["-c", "import time; time.sleep(120)"])
    limpieza.append(ajeno)
    salida = tmp_path / "hijo.pid"
    padre = _lanzar(["-c", _PADRE_QUE_YA_TIENE_HIJO, str(salida)])
    limpieza.append(padre)
    hijo_pid = _esperar_pid(salida)
    limpieza.append(hijo_pid)
    handle_del_padre = _sondas().abrir_proceso(padre.pid)
    try:
        job.assign(handle_del_padre)

        # Act
        job.adopt_descendants(handle_del_padre)
        job.terminate()

        # Assert: murió el árbol del lanzado, no el proceso ajeno.
        assert _esperar_muerte(padre.pid)
        assert _esperar_muerte(hijo_pid)
        assert psutil.pid_exists(ajeno.pid), "un proceso ajeno al árbol no puede entrar al Job ni morir con él"
    finally:
        _sondas().cerrar_handle(handle_del_padre)


# ---------------------------------------------------------------------------
# PPID reciclado de un proceso de sistema que NO se puede abrir (flake de CI: «[720, 844] ... GetLastError=5»)
# ---------------------------------------------------------------------------

#: Segundos entre 1601-01-01 (época de FILETIME) y 1970-01-01.
_SEGUNDOS_1601_A_1970 = 11_644_473_600


def _api_real(job: Win32JobObject) -> puente._Win32AdoptionApi:
    """La capa ``ctypes`` de producción, la misma que arma ``Win32JobObject.adopt_descendants``."""
    return puente._Win32AdoptionApi(job._kernel32, job._handle)


def _no_se_puede_abrir(api: puente._Win32AdoptionApi, pid: int) -> bool:
    try:
        handle = api.open_process(pid)
    except BridgeCommandError:
        return True
    if handle is not None:
        api.close(handle)
    return False


def _procesos_viejos_que_no_se_pueden_abrir(api: puente._Win32AdoptionApi, *, mas_viejos_que: int) -> list[int]:
    """Procesos (``csrss``, ``wininit``, ...) que existían antes que ``mas_viejos_que`` y el bridge no puede abrir."""
    limite = psutil.Process(mas_viejos_que).create_time()
    encontrados: list[int] = []
    for pid, _ppid in api.processes():
        if pid <= 4 or not _no_se_puede_abrir(api, pid):  # 0 y 4 son Idle y System: nada que comparar
            continue
        with contextlib.suppress(psutil.Error):
            if psutil.Process(pid).create_time() < limite:
                encontrados.append(pid)
    return encontrados


def test_la_creacion_leida_sin_abrir_el_proceso_coincide_con_la_leida_por_handle(limpieza, job) -> None:
    """Valida la disposición de la estructura del sistema contra ``GetProcessTimes``, bit a bit, en el kernel real."""
    # Arrange
    api = _api_real(job)
    dormido = _lanzar(["-c", "import time; time.sleep(120)"])
    limpieza.append(dormido)

    for pid in (os.getpid(), dormido.pid):
        handle = api.open_process(pid)
        assert handle is not None
        try:
            por_handle = api.creation_time(handle)
        finally:
            api.close(handle)

        # Act / Assert
        assert api.creation_time_by_pid(pid) == por_handle, f"la creación de {pid} no coincide"


def test_la_creacion_de_un_pid_que_no_existe_es_desconocida_y_no_un_error(job) -> None:
    assert _api_real(job).creation_time_by_pid(2**31 - 8) is None


def test_la_creacion_de_un_proceso_que_no_se_puede_abrir_se_lee_del_sistema_y_coincide_con_psutil(
    limpieza, job
) -> None:
    # Arrange: un proceso de sistema real que el bridge no puede abrir (ACCESS_DENIED), más viejo que uno recién lanzado.
    api = _api_real(job)
    reciente = _lanzar(["-c", "import time; time.sleep(120)"])
    limpieza.append(reciente)
    inabribles = _procesos_viejos_que_no_se_pueden_abrir(api, mas_viejos_que=reciente.pid)
    if not inabribles:
        pytest.skip("este Windows no tiene procesos viejos inabribles para este token: no hay qué medir")

    # Act
    leida = api.creation_time_by_pid(inabribles[0])

    # Assert: la misma marca de tiempo que ve psutil (que usa la instantánea del sistema cuando el proceso no se abre).
    assert leida is not None
    esperada = psutil.Process(inabribles[0]).create_time()
    assert abs((leida / 10_000_000 - _SEGUNDOS_1601_A_1970) - esperada) < 0.01


def test_un_proceso_de_sistema_inabrible_con_ppid_reciclado_no_hace_fallar_la_adopcion_real(limpieza, job) -> None:
    """REPRODUCE el flake de CI con el kernel real: ``OpenProcess`` del proceso viejo falla con ``GetLastError=5``.

    El PID reciclado no se puede provocar a voluntad (el asignador de PIDs es LIFO), así que se inyecta el PPID en la
    única capa que lo lee del sistema (``processes``): lo demás —``OpenProcess``, la creación, el Job— es real.
    """
    # Arrange
    dormido = _lanzar(["-c", "import time; time.sleep(120)"])
    limpieza.append(dormido)
    real = _api_real(job)
    viejos = _procesos_viejos_que_no_se_pueden_abrir(real, mas_viejos_que=dormido.pid)
    if not viejos:
        pytest.skip("este Windows no tiene procesos viejos inabribles para este token: no hay qué reproducir")
    viejo = viejos[0]

    class _ApiConPidReciclado(puente._Win32AdoptionApi):
        def processes(self) -> list[tuple[int, int]]:
            return [(pid, dormido.pid if pid == viejo else ppid) for pid, ppid in super().processes()]

    api = _ApiConPidReciclado(job._kernel32, job._handle)
    assert (viejo, dormido.pid) in api.processes(), "control: el proceso viejo aparece como hijo del lanzado"
    with pytest.raises(BridgeCommandError, match=r"GetLastError=5"):
        api.open_process(viejo)  # control: es el mismo ACCESS_DENIED que vio CI
    handle_del_lanzado = _sondas().abrir_proceso(dormido.pid)
    try:
        job.assign(handle_del_lanzado)

        # Act: antes del arreglo esto lanzaba ``BridgeTerminationError`` («quedan vivos fuera del Job: [<viejo>]»).
        adoptados = adopt_descendants(api, handle_del_lanzado)

        # Assert: el proceso de sistema no es un descendiente: ni se adopta ni se toca.
        assert adoptados == 0
        assert psutil.pid_exists(viejo), "el proceso de sistema ajeno no puede morir"
    finally:
        _sondas().cerrar_handle(handle_del_lanzado)


def test_active_processes_sobre_un_job_cerrado_falla_con_un_error_del_bridge() -> None:
    cerrado = Win32JobObject(generar_nombre_de_job())
    cerrado.close()

    with pytest.raises(BridgeCommandError, match="cerrado"):
        cerrado.active_processes()
    with pytest.raises(BridgeCommandError, match="cerrado"):
        cerrado.adopt_descendants(1)
