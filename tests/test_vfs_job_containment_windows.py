"""Contención con Job Objects REALES de Windows, con procesos inocuos (``python.exe`` durmiendo).

Las pruebas sintéticas del protocolo (``test_vfs_supervision_fence.py``) no sustituyen
la verificación de contención del sistema operativo: acá se ejercitan los Job Objects
de verdad, sin MO2, sin USVFS y sin herramientas de modding.

Qué fija este módulo (todas son propiedades de Windows, no de Sky-Claw):

* un descendiente creado ANTES de ``AssignProcessToJobObject`` queda FUERA del Job y
  sobrevive a ``TerminateJobObject`` — es el peligro que el fence de supervisión evita
  impidiendo que el worker cree nada antes de la confirmación del bridge;
* uno creado DESPUÉS queda contenido y muere con el Job;
* ``IsProcessInJob(proceso, NULL)`` responde por pertenencia a CUALQUIER Job: no prueba
  pertenencia al Job de NUESTRO bridge (hay que preguntar contra el handle propio);
* un proceso ya incluido en un Job ajeno admite además el Job propio (Jobs anidados);
* el ``BridgeLaunchController`` de producción, con el ``Win32JobObject`` real, contiene
  al worker y a sus descendientes, y los termina al cancelar.

Se omite fuera de Windows con motivo explícito: un skip NO cuenta como PASS; la evidencia
vive en la corrida del sandbox Windows de CI.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import subprocess
import sys
import textwrap
import time

import psutil
import pytest

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import PROTOCOL_VERSION, BridgeLaunchController

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Job Objects reales: solo Windows (la evidencia vive en el sandbox Windows de CI)",
)

REPO = pathlib.Path(__file__).resolve().parents[1]

#: El padre espera un archivo-bandera ANTES de crear a su hijo: el test decide si la
#: asignación al Job ocurre antes o después de que nazca el descendiente.
_PADRE_QUE_ESPERA_LA_BANDERA = textwrap.dedent(
    """
    import os, subprocess, sys, time
    bandera, salida = sys.argv[1], sys.argv[2]
    while not os.path.exists(bandera):
        time.sleep(0.01)
    hijo = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(salida, "w").write(str(hijo.pid))
    time.sleep(120)
    """
)

#: El padre crea a su hijo apenas arranca (como un worker que despacha sin esperar).
_PADRE_QUE_YA_TIENE_HIJO = textwrap.dedent(
    """
    import subprocess, sys, time
    hijo = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(sys.argv[1], "w").write(str(hijo.pid))
    time.sleep(120)
    """
)


def _sondas():
    """Import diferido: el módulo del laboratorio solo es utilizable en Windows."""
    from scripts import windows_job_probe

    return windows_job_probe


def _esperar_pid(ruta: pathlib.Path, *, timeout: float = 20.0) -> int:
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if ruta.is_file():
            texto = ruta.read_text(encoding="utf-8").strip()
            if texto:
                return int(texto)
        time.sleep(0.02)
    raise AssertionError(f"no apareció {ruta.name} en {timeout}s")


def _esperar_muerte(pid: int, *, timeout: float = 10.0) -> bool:
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.05)
    return not psutil.pid_exists(pid)


@pytest.fixture
def limpieza():
    """Mata, pase lo que pase, todo proceso que el test haya registrado."""
    pids: list[int] = []
    yield pids
    sondas = _sondas()
    for pid in pids:
        sondas.matar_arbol(pid)


def _asignar_pid(sondas, job: int, pid: int) -> None:
    handle = sondas.abrir_proceso(pid)
    try:
        sondas.asignar_a_job(job, handle)
    finally:
        sondas.cerrar_handle(handle)


def _en_job(sondas, job: int | None, pid: int) -> bool:
    handle = sondas.abrir_proceso(pid)
    try:
        return sondas.proceso_en_job(handle, job)
    finally:
        sondas.cerrar_handle(handle)


# ---------------------------------------------------------------------------
# Semántica del sistema operativo
# ---------------------------------------------------------------------------


def test_un_descendiente_creado_antes_de_la_asignacion_queda_fuera_del_job(tmp_path, limpieza) -> None:
    # Arrange: el "worker" ya creó a su hijo cuando el bridge recién lo asigna.
    sondas = _sondas()
    salida = tmp_path / "hijo.pid"
    with sondas.job_temporal() as job:
        padre = sondas.lanzar_como_mo2([sys.executable, "-c", _PADRE_QUE_YA_TIENE_HIJO, str(salida)])
        limpieza.append(padre.pid)
        hijo_pid = _esperar_pid(salida)
        limpieza.append(hijo_pid)

        # Act
        _asignar_pid(sondas, job, padre.pid)
        pids = sondas.pids_del_job(job)
        sondas.terminar_job(job)

        # Assert: el padre estaba y murió; el hijo previo NUNCA fue adoptado y sobrevive.
        assert padre.pid in pids
        assert hijo_pid not in pids
        assert _esperar_muerte(padre.pid)
        assert psutil.pid_exists(hijo_pid), "el descendiente previo a la asignación sobrevivió al Job"


def test_un_descendiente_creado_despues_de_la_asignacion_queda_contenido_y_muere_con_el_job(tmp_path, limpieza) -> None:
    # Arrange: el padre espera la bandera, así que su hijo nace DESPUÉS de la asignación.
    sondas = _sondas()
    bandera, salida = tmp_path / "go", tmp_path / "hijo.pid"
    with sondas.job_temporal() as job:
        padre = sondas.lanzar_como_mo2([sys.executable, "-c", _PADRE_QUE_ESPERA_LA_BANDERA, str(bandera), str(salida)])
        limpieza.append(padre.pid)
        _asignar_pid(sondas, job, padre.pid)

        # Act
        bandera.write_text("go", encoding="utf-8")
        hijo_pid = _esperar_pid(salida)
        limpieza.append(hijo_pid)
        pids = sondas.pids_del_job(job)
        sondas.terminar_job(job)

        # Assert
        assert {padre.pid, hijo_pid} <= set(pids)
        assert _esperar_muerte(padre.pid)
        assert _esperar_muerte(hijo_pid), "el descendiente posterior a la asignación debía morir con el Job"


def test_is_process_in_job_sin_job_responde_true_para_un_job_ajeno(tmp_path, limpieza) -> None:
    """``IsProcessInJob(p, NULL)`` NO prueba pertenencia al Job del bridge: contesta por cualquier Job."""
    # Arrange: el proceso está en un Job IMPOSTOR y NO en el nuestro.
    sondas = _sondas()
    with sondas.job_temporal() as nuestro, sondas.job_temporal() as impostor:
        proceso = sondas.lanzar_como_mo2([sys.executable, "-c", "import time; time.sleep(120)"])
        limpieza.append(proceso.pid)
        _asignar_pid(sondas, impostor, proceso.pid)

        # Act
        en_cualquiera = _en_job(sondas, None, proceso.pid)
        en_nuestro = _en_job(sondas, nuestro, proceso.pid)
        en_impostor = _en_job(sondas, impostor, proceso.pid)

        # Assert: la pregunta correcta es contra el handle propio.
        assert en_cualquiera is True
        assert en_nuestro is False
        assert en_impostor is True


def test_un_proceso_en_un_job_ajeno_admite_ademas_el_job_propio(tmp_path, limpieza) -> None:
    """Jobs anidados (Windows 8+): el Job propio puede terminar a un proceso ya incluido en otro."""
    # Arrange: el Job ajeno NO tiene kill-on-close (como el de un lanzador cualquiera).
    sondas = _sondas()
    with sondas.job_temporal(kill_on_close=False) as ajeno, sondas.job_temporal() as nuestro:
        proceso = sondas.lanzar_como_mo2([sys.executable, "-c", "import time; time.sleep(120)"])
        limpieza.append(proceso.pid)
        _asignar_pid(sondas, ajeno, proceso.pid)

        # Act
        _asignar_pid(sondas, nuestro, proceso.pid)
        sondas.terminar_job(nuestro)

        # Assert
        assert _esperar_muerte(proceso.pid), "el Job propio debía poder terminar al proceso pese al Job ajeno"


def test_un_proceso_puede_consultar_su_propio_job_y_ver_a_su_lanzador(tmp_path, limpieza, monkeypatch) -> None:
    """Base de una auto-verificación del worker: ve sus compañeros de Job y sus límites."""
    # Arrange: un hijo que reporta lo que ve de SU Job (consulta con handle NULL).
    monkeypatch.chdir(REPO)  # el hijo importa `scripts.windows_job_probe` desde el cwd
    sondas = _sondas()
    informe = tmp_path / "informe.json"
    codigo = (
        "import json, os, sys;"
        "from scripts import windows_job_probe as w;"
        "open(sys.argv[1], 'w').write(json.dumps({"
        "'pid': os.getpid(), 'pids': list(w.pids_del_job(None)), 'kill_on_close': w.job_tiene_kill_on_close(None)}))"
    )
    with sondas.job_temporal() as job:
        proceso = sondas.lanzar_como_mo2(
            [sys.executable, "-c", f"import time; time.sleep(0.5); {codigo}", str(informe)]
        )
        limpieza.append(proceso.pid)
        _asignar_pid(sondas, job, proceso.pid)

        # Act
        proceso.wait(timeout=60)
        visto = json.loads(informe.read_text(encoding="utf-8"))

        # Assert: coincide con la vista de afuera y declara kill-on-close.
        assert visto["pid"] == proceso.pid
        assert proceso.pid in visto["pids"]
        assert visto["kill_on_close"] is True


# ---------------------------------------------------------------------------
# El BridgeLaunchController de producción con el Win32JobObject REAL
# ---------------------------------------------------------------------------


class _OrganizerQueLanzaProcesos:
    """``startApplication`` como la API de MO2: crea el proceso CORRIENDO y devuelve su HANDLE."""

    def __init__(self, argv: list[str], *, retardo_antes_de_devolver_s: float = 0.0) -> None:
        self._argv = argv
        self._retardo = retardo_antes_de_devolver_s
        self.proceso: subprocess.Popen[bytes] | None = None

    def startApplication(self, *_args: object) -> int:  # noqa: N802 - API de MO2
        sondas = _sondas()
        self.proceso = sondas.lanzar_como_mo2(self._argv)
        if self._retardo:
            time.sleep(self._retardo)
        return sondas.abrir_proceso(self.proceso.pid)

    def waitForApplication(self, handle: int, refresh: bool) -> tuple[bool, int]:  # noqa: N802 - API de MO2
        assert self.proceso is not None
        return True, int(self.proceso.wait())


def _controlador_real(tmp_path: pathlib.Path, organizer: _OrganizerQueLanzaProcesos, eventos: list[dict]):
    jobs = tmp_path / "jobs"
    jobs.mkdir(exist_ok=True)
    manifiesto = jobs / "job-1.json"
    manifiesto.write_text("{}", encoding="utf-8")
    worker = tmp_path / "worker.exe"
    worker.write_bytes(b"MZ")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    controlador = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        jobs_root=jobs,
        send_event=eventos.append,
        process_waiter=lambda handle: organizer.waitForApplication(handle, False),
    )
    mensaje = {
        "protocol_version": PROTOCOL_VERSION,
        "type": "launch_worker",
        "job_id": "job-1",
        "profile": "Default",
        "manifest_path": str(manifiesto),
        "overwrite_mod": None,
    }
    cancelar = {"protocol_version": PROTOCOL_VERSION, "type": "cancel", "job_id": "job-1"}
    return controlador, mensaje, cancelar


def test_el_bridge_real_contiene_al_worker_y_a_sus_descendientes_en_el_job_correcto(tmp_path, limpieza) -> None:
    # Arrange: el worker no crea a su hijo hasta que el bridge ya lo asignó (lo que garantiza el fence).
    bandera, salida = tmp_path / "go", tmp_path / "hijo.pid"
    organizer = _OrganizerQueLanzaProcesos(
        [sys.executable, "-c", _PADRE_QUE_ESPERA_LA_BANDERA, str(bandera), str(salida)]
    )
    eventos: list[dict] = []
    controlador, mensaje, cancelar = _controlador_real(tmp_path, organizer, eventos)

    # Act
    controlador.launch(mensaje)
    assert organizer.proceso is not None
    worker_pid = organizer.proceso.pid
    limpieza.append(worker_pid)
    assert eventos[0]["type"] == "launch_ack"
    bandera.write_text("go", encoding="utf-8")
    hijo_pid = _esperar_pid(salida)
    limpieza.append(hijo_pid)
    vivos_antes = psutil.pid_exists(worker_pid) and psutil.pid_exists(hijo_pid)
    controlador.cancel(cancelar)
    controlador.wait_for_monitors(timeout=15)

    # Assert: el cancel del bridge termina al worker Y a su descendiente (mismo Job).
    assert vivos_antes
    assert _esperar_muerte(worker_pid)
    assert _esperar_muerte(hijo_pid)
    assert eventos[-1]["event"] == "worker_exit"


def test_con_una_asignacion_tardia_el_bridge_real_deja_un_descendiente_fuera_del_job(tmp_path, limpieza) -> None:
    """Caracteriza el PELIGRO que el fence de supervisión cierra del lado del worker.

    ``startApplication`` tarda en devolver el HANDLE (planificación, carga) y el worker ya
    despachó: su hijo nace antes de ``assign``. El cancel del bridge mata al worker y deja
    vivo a ese descendiente. Con el fence el worker no crea nada hasta el ``launch_ack``.
    """
    # Arrange
    salida = tmp_path / "hijo.pid"
    organizer = _OrganizerQueLanzaProcesos(
        [sys.executable, "-c", _PADRE_QUE_YA_TIENE_HIJO, str(salida)], retardo_antes_de_devolver_s=2.0
    )
    eventos: list[dict] = []
    controlador, mensaje, cancelar = _controlador_real(tmp_path, organizer, eventos)

    # Act
    controlador.launch(mensaje)
    assert organizer.proceso is not None
    worker_pid = organizer.proceso.pid
    limpieza.append(worker_pid)
    hijo_pid = _esperar_pid(salida)
    limpieza.append(hijo_pid)
    controlador.cancel(cancelar)
    controlador.wait_for_monitors(timeout=15)

    # Assert
    assert _esperar_muerte(worker_pid)
    assert psutil.pid_exists(hijo_pid), "el descendiente previo a la asignación quedó fuera del Job"
    with contextlib.suppress(psutil.Error):
        psutil.Process(hijo_pid).kill()
