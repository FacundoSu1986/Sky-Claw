"""Identidad del Job con procesos REALES de Windows: la semántica que un doble no puede probar.

``test_vfs_job_identity.py`` fija la lógica de decisión con una API falsa. Esto prueba lo que solo el sistema
operativo responde:

* el verificador, dentro de un proceso real, acepta SOLO el Job con el nombre pedido: no uno ajeno (donde
  ``IsProcessInJob(p, NULL)`` respondería ``TRUE``), no ninguno;
* un descendiente vivo fuera del Job hace fallar la verificación y el limpiador lo mata de verdad;
* el worker NO retiene su handle: ``KILL_ON_JOB_CLOSE`` solo dispara al cerrarse el ÚLTIMO handle, y el control
  negativo de este módulo mide que un handle retenido lo impide;
* el Job nativo se crea con su nombre, rechaza un nombre preexistente (squatting, otro tipo de objeto) y su ACL
  solo concede consulta.

El módulo de identidad se carga POR RUTA en los hijos: importar ``sky_claw`` ejecuta el ``__init__`` del paquete,
que crea los ``cmd.exe /c ver`` del arranque y contaminaría lo que se quiere medir. Se salta fuera de Windows:
un skip es «no verificado», nunca un PASS (el workflow del sandbox corre con ``-rA``).
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import psutil
import pytest

from scripts import windows_job_probe as sonda  # importable en cualquier plataforma; sus llamadas exigen Windows
from sky_claw.local.mo2 import vfs_job_identity
from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import BridgeCommandError, Win32JobObject
from sky_claw.local.mo2.vfs_job_identity import generar_nombre_de_job

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Job Objects reales: solo Windows")

_ERROR_ACCESS_DENIED = 5
_JOB_OBJECT_ASSIGN_PROCESS = 0x0001
_JOB_OBJECT_SET_ATTRIBUTES = 0x0002
_JOB_OBJECT_QUERY = 0x0004
_JOB_OBJECT_TERMINATE = 0x0008
_RUTA_DEL_MODULO = str(pathlib.Path(vfs_job_identity.__file__).resolve())
#: En un venv de ``uv``, ``sys.executable`` es el trampoline ``venvlauncher.exe`` (que crea un proceso hijo ``python.exe``
#: antes de devolver ``LISTO``). Usar ``sys._base_executable`` + ``PYTHONPATH`` asegura que ``_Hijo.proceso.pid`` sea
#: el proceso Python real que ejecuta ``verificar_identidad_del_job``.
PYTHON_DIRECTO = getattr(sys, "_base_executable", sys.executable)
_ENTORNO_DIRECTO = {
    **os.environ,
    "PYTHONPATH": os.pathsep.join([str(pathlib.Path(__file__).resolve().parents[1]), *(p for p in sys.path if p)]),
}

#: Hijo de prueba. Carga el verificador por ruta, avisa (``LISTO``) y espera la bandera ``go`` ANTES de verificar:
#: así el test puede asignarlo (o no) a un Job entre medio. ``hijo_previo`` crea un nieto ANTES de avisar.
_HIJO = r"""
import importlib.util, json, os, subprocess, sys, time
spec = importlib.util.spec_from_file_location("vji", sys.argv[1])
vji = importlib.util.module_from_spec(spec)
sys.modules["vji"] = vji
spec.loader.exec_module(vji)
nombre, bandera, modo = sys.argv[2], sys.argv[3], sys.argv[4]
if modo == "hijo_previo":
    nieto = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    print("NIETO", nieto.pid, flush=True)
if modo == "retener_handle":
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenJobObjectW.restype = ctypes.c_void_p
    retenido = k32.OpenJobObjectW(0x0004, False, nombre)
    print("RETENIDO", bool(retenido), flush=True)
print("LISTO", flush=True)
while not os.path.exists(bandera):
    time.sleep(0.02)
if modo == "retener_handle":
    time.sleep(120)
try:
    prueba = vji.verificar_identidad_del_job(nombre)
except vji.JobIdentityError as exc:
    print("FALLO", str(exc).replace("\n", " "), flush=True)
    print("LIMPIADOS", sorted(vji.terminar_descendientes_vivos()), flush=True)
    sys.exit(72)
print("OK", json.dumps({"nombre": prueba.nombre, "totales": prueba.procesos_totales, "activos": prueba.procesos_activos}), flush=True)
if modo == "dormir":
    time.sleep(120)
"""


class _Hijo:
    """Proceso hijo real con su salida leída desde un hilo (sin bloquear al test)."""

    def __init__(self, tmp_path: pathlib.Path, nombre: str, modo: str) -> None:
        self.bandera = tmp_path / f"go-{os.getpid()}-{time.monotonic_ns()}"
        self.lineas: queue.Queue[str] = queue.Queue()
        self.proceso = subprocess.Popen(  # noqa: S603 - argv fijo del test
            [PYTHON_DIRECTO, "-c", _HIJO, _RUTA_DEL_MODULO, nombre, str(self.bandera), modo],
            env=_ENTORNO_DIRECTO,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._hilo = threading.Thread(target=self._leer, daemon=True)
        self._hilo.start()
        self.nieto: int | None = None

    def _leer(self) -> None:
        assert self.proceso.stdout is not None
        for linea in self.proceso.stdout:
            self.lineas.put(linea.rstrip("\n"))

    def esperar(self, prefijo: str, *, timeout: float = 60.0) -> str:
        limite = time.monotonic() + timeout
        while time.monotonic() < limite:
            try:
                linea = self.lineas.get(timeout=0.2)
            except queue.Empty:
                if self.proceso.poll() is not None and self.lineas.empty():
                    break
                continue
            if linea.startswith("NIETO"):
                self.nieto = int(linea.split()[1])
            if linea.startswith(prefijo):
                return linea
        raise AssertionError(f"el hijo no emitió {prefijo!r} a tiempo (rc={self.proceso.poll()})")

    def pids(self) -> list[int]:
        return [self.proceso.pid, *([self.nieto] if self.nieto else [])]

    def asignar_a(self, job: Any) -> None:
        handle = sonda.abrir_proceso(self.proceso.pid)
        try:
            job.assign(handle)
        finally:
            sonda.cerrar_handle(handle)

    def soltar(self) -> None:
        self.bandera.write_text("go", encoding="utf-8")

    def cerrar(self) -> None:
        """Cierra el pipe de stdout (un FileIO abierto es un ``ResourceWarning``, elevado a error en este repo)."""
        self._hilo.join(timeout=10)
        if self.proceso.stdout is not None:
            self.proceso.stdout.close()


@pytest.fixture
def hijos(tmp_path: pathlib.Path) -> Iterator[list[_Hijo]]:
    """Registro de hijos: al terminar el test se matan ellos y sus descendientes (el laboratorio no deja residuos)."""
    creados: list[_Hijo] = []
    yield creados
    for hijo in creados:
        for pid in hijo.pids():
            with contextlib.suppress(psutil.Error):
                for descendiente in psutil.Process(pid).children(recursive=True):
                    descendiente.kill()
                psutil.Process(pid).kill()
        with contextlib.suppress(Exception):
            hijo.proceso.wait(timeout=10)
        hijo.cerrar()


def _hijo(hijos: list[_Hijo], tmp_path: pathlib.Path, nombre: str, modo: str = "verificar") -> _Hijo:
    nuevo = _Hijo(tmp_path, nombre, modo)
    hijos.append(nuevo)
    nuevo.esperar("LISTO")
    return nuevo


# ---------------------------------------------------------------------------
# 1. El verificador contra procesos reales
# ---------------------------------------------------------------------------


def test_un_worker_dentro_del_job_con_el_nombre_pedido_se_verifica(tmp_path: pathlib.Path, hijos: list[_Hijo]) -> None:
    # Arrange
    nombre = generar_nombre_de_job()
    job = Win32JobObject(nombre)
    try:
        hijo = _hijo(hijos, tmp_path, nombre)
        hijo.asignar_a(job)
        # Act
        hijo.soltar()
        resultado = hijo.esperar("OK")
        # Assert: la línea es `OK <json>` y el JSON escapa `\\`; se compara el valor parseado, no el texto.
        assert hijo.proceso.wait(timeout=30) == 0
        assert json.loads(resultado.removeprefix("OK "))["nombre"] == nombre
    finally:
        job.close()


def test_un_worker_fuera_de_todo_job_con_ese_nombre_falla_cerrado(tmp_path: pathlib.Path, hijos: list[_Hijo]) -> None:
    # Arrange: el Job con ese nombre EXISTE pero el worker no fue asignado a él (el hijo Python que nació antes del assign).
    nombre = generar_nombre_de_job()
    job = Win32JobObject(nombre)
    try:
        hijo = _hijo(hijos, tmp_path, nombre)
        # Act
        hijo.soltar()
        fallo = hijo.esperar("FALLO")
        # Assert
        assert "no pertenece" in fallo
        assert hijo.proceso.wait(timeout=30) == 72
    finally:
        job.close()


def test_estar_en_un_job_ajeno_no_basta_aunque_is_process_in_job_con_null_diga_que_si(
    tmp_path: pathlib.Path, hijos: list[_Hijo]
) -> None:
    # Arrange: el hijo vive en OTRO Job (anónimo); el Job con el nombre pedido existe pero está vacío.
    nombre = generar_nombre_de_job()
    job_pedido = Win32JobObject(nombre)
    ajeno = sonda.crear_job()
    try:
        hijo = _hijo(hijos, tmp_path, nombre)
        handle = sonda.abrir_proceso(hijo.proceso.pid)
        try:
            sonda.asignar_a_job(ajeno, handle)
            # Premisa medida (#856 §4.1): contra NULL, pertenecer a CUALQUIER Job da TRUE. Es la trampa.
            assert sonda.proceso_en_job(handle, None) is True
        finally:
            sonda.cerrar_handle(handle)
        # Act
        hijo.soltar()
        fallo = hijo.esperar("FALLO")
        # Assert: el verificador pregunta contra el handle del Job pedido, y ahí es FALSE.
        assert "no pertenece" in fallo
        assert hijo.proceso.wait(timeout=30) == 72
    finally:
        sonda.cerrar_handle(ajeno)
        job_pedido.close()


def test_un_worker_en_un_job_anidado_dentro_de_uno_ajeno_si_se_verifica(
    tmp_path: pathlib.Path, hijos: list[_Hijo]
) -> None:
    # Arrange: caso real de MO2 (o del runner) que ya mete sus hijos en un Job propio: el del bridge se ANIDA.
    nombre = generar_nombre_de_job()
    job = Win32JobObject(nombre)
    ajeno = sonda.crear_job()
    try:
        hijo = _hijo(hijos, tmp_path, nombre)
        handle = sonda.abrir_proceso(hijo.proceso.pid)
        try:
            sonda.asignar_a_job(ajeno, handle)
        finally:
            sonda.cerrar_handle(handle)
        hijo.asignar_a(job)
        # Act
        hijo.soltar()
        hijo.esperar("OK")
        # Assert
        assert hijo.proceso.wait(timeout=30) == 0
    finally:
        sonda.cerrar_handle(ajeno)
        job.close()


def test_un_descendiente_previo_fuera_del_job_falla_y_el_limpiador_lo_mata(
    tmp_path: pathlib.Path, hijos: list[_Hijo]
) -> None:
    # Arrange: el nieto nace ANTES de que el padre entre al Job (lo que el assign tardío deja fuera).
    nombre = generar_nombre_de_job()
    job = Win32JobObject(nombre)
    try:
        hijo = _hijo(hijos, tmp_path, nombre, "hijo_previo")
        assert hijo.nieto is not None
        nieto = psutil.Process(hijo.nieto)
        hijo.asignar_a(job)
        assert nieto.is_running()
        # Act
        hijo.soltar()
        fallo = hijo.esperar("FALLO")
        limpiados = hijo.esperar("LIMPIADOS")
        # Assert: el padre SÍ está en el Job, pero su nieto no: se detecta, se mata y el worker no sigue.
        assert str(hijo.nieto) in fallo
        assert str(hijo.nieto) in limpiados
        assert hijo.proceso.wait(timeout=30) == 72
        time.sleep(0.5)
        assert not psutil.pid_exists(hijo.nieto) or not nieto.is_running(), "el nieto fuera del Job debía morir"
    finally:
        job.close()


def test_un_nombre_valido_de_un_job_que_no_existe_falla_cerrado_con_el_error_de_win32() -> None:
    # Arrange: formato correcto pero nadie creó ese Job (bridge viejo con Job anónimo, o nombre equivocado).
    nombre = generar_nombre_de_job()
    # Act / Assert: ERROR_FILE_NOT_FOUND (2) llega como un error de identidad, no como una prueba.
    with pytest.raises(vfs_job_identity.JobIdentityError, match=r"OpenJobObjectW falló \(GetLastError=2\)"):
        vfs_job_identity.verificar_identidad_del_job(nombre)


def test_un_objeto_de_otro_tipo_con_ese_nombre_no_se_confunde_con_el_job() -> None:
    # Arrange: un evento llamado como el Job pedido. OpenJobObjectW solo abre Jobs → falla, no devuelve el evento.
    nombre = generar_nombre_de_job()
    k32 = sonda._kernel32()
    k32.CreateEventW.restype = sonda._ctypes_win.c_void_p
    evento = k32.CreateEventW(None, True, False, nombre)
    assert evento
    try:
        # Act / Assert
        with pytest.raises(vfs_job_identity.JobIdentityError):
            vfs_job_identity.verificar_identidad_del_job(nombre)
    finally:
        k32.CloseHandle(evento)


# ---------------------------------------------------------------------------
# 2. El handle: KILL_ON_JOB_CLOSE solo dispara al cerrarse el último
# ---------------------------------------------------------------------------


def test_el_worker_no_retiene_el_handle_y_cerrar_el_job_del_bridge_lo_mata(
    tmp_path: pathlib.Path, hijos: list[_Hijo]
) -> None:
    # Arrange: el worker se verifica y SIGUE VIVO (dormir).
    nombre = generar_nombre_de_job()
    job = Win32JobObject(nombre)
    hijo = _hijo(hijos, tmp_path, nombre, "dormir")
    hijo.asignar_a(job)
    hijo.soltar()
    hijo.esperar("OK")
    assert hijo.proceso.poll() is None
    # Act: el bridge cierra SU handle (lo que ocurre si MO2 muere).
    job.close()
    # Assert: si el verificador hubiera retenido el suyo, el proceso seguiría vivo.
    assert hijo.proceso.wait(timeout=20) is not None


def test_control_negativo_un_handle_retenido_impide_que_cerrar_el_job_mate_al_proceso(
    tmp_path: pathlib.Path, hijos: list[_Hijo]
) -> None:
    # Arrange: este hijo abre el Job por nombre y NO cierra su handle (lo que hace un worker defectuoso).
    nombre = generar_nombre_de_job()
    job = Win32JobObject(nombre)
    hijo = _hijo(hijos, tmp_path, nombre, "retener_handle")
    hijo.asignar_a(job)
    hijo.soltar()
    # Act
    job.close()
    time.sleep(3.0)
    # Assert: sigue vivo → la premisa del diseño (hay que cerrar el handle) es real, y el test anterior mide algo.
    assert hijo.proceso.poll() is None, "con un handle retenido KILL_ON_JOB_CLOSE no debía disparar"


# ---------------------------------------------------------------------------
# 3. El Job nativo del bridge
# ---------------------------------------------------------------------------


def _abrir(acceso: int, nombre: str) -> tuple[int, int]:
    k32 = sonda._kernel32()
    handle = k32.OpenJobObjectW(acceso, False, nombre)
    error = sonda._ctypes_win.get_last_error()
    if handle:
        k32.CloseHandle(handle)
    return int(handle or 0), error


def test_el_job_nativo_se_abre_para_consulta_y_rechaza_terminar_cambiar_limites_o_asignar() -> None:
    # Arrange
    nombre = generar_nombre_de_job()
    job = Win32JobObject(nombre)
    try:
        # Act / Assert: consulta permitida...
        handle, _ = _abrir(_JOB_OBJECT_QUERY, nombre)
        assert handle != 0
        # ...y ningún otro derecho: quien lo abra no puede quitarle KILL_ON_JOB_CLOSE ni terminarlo ni meterle procesos.
        for derecho in (_JOB_OBJECT_TERMINATE, _JOB_OBJECT_SET_ATTRIBUTES, _JOB_OBJECT_ASSIGN_PROCESS):
            handle, error = _abrir(derecho, nombre)
            assert handle == 0, f"el derecho {derecho:#x} no debía concederse"
            assert error == _ERROR_ACCESS_DENIED
    finally:
        job.close()


def test_el_job_nativo_tiene_kill_on_close_y_ningun_breakaway() -> None:
    # Arrange
    nombre = generar_nombre_de_job()
    job = Win32JobObject(nombre)
    try:
        # Act
        k32 = sonda._kernel32()
        handle = k32.OpenJobObjectW(_JOB_OBJECT_QUERY, False, nombre)
        assert handle
        try:
            banderas = vfs_job_identity._ApiWin32().banderas_de_limite(int(handle))
        finally:
            k32.CloseHandle(handle)
        # Assert
        assert banderas & 0x2000, "KILL_ON_JOB_CLOSE"
        assert not banderas & (0x0800 | 0x1000), "ni BREAKAWAY_OK ni SILENT_BREAKAWAY_OK"
    finally:
        job.close()


def test_crear_dos_veces_el_mismo_nombre_falla_y_no_adopta_el_job_existente() -> None:
    # Arrange
    nombre = generar_nombre_de_job()
    primero = Win32JobObject(nombre)
    try:
        # Act / Assert
        with pytest.raises(BridgeCommandError, match="ya exist"):
            Win32JobObject(nombre)
        # El primero sigue intacto: el segundo intento no le cerró ni le cambió nada.
        handle, _ = _abrir(_JOB_OBJECT_QUERY, nombre)
        assert handle != 0
    finally:
        primero.close()


def test_un_job_preexistente_de_un_squatter_no_se_adopta() -> None:
    # Arrange: alguien creó antes un Job con ese nombre (y su DACL por defecto, mucho más permisiva).
    nombre = generar_nombre_de_job()
    squatter = sonda.crear_job(nombre=nombre, kill_on_close=False)
    try:
        # Act / Assert
        with pytest.raises(BridgeCommandError, match="ya exist"):
            Win32JobObject(nombre)
    finally:
        sonda.cerrar_handle(squatter)


def test_un_objeto_de_otro_tipo_con_el_mismo_nombre_tambien_falla_cerrado() -> None:
    # Arrange: un evento con el nombre pedido (no es un Job): CreateJobObjectW falla con ERROR_INVALID_HANDLE.
    nombre = generar_nombre_de_job()
    k32 = sonda._kernel32()
    k32.CreateEventW.restype = sonda._ctypes_win.c_void_p
    evento = k32.CreateEventW(None, True, False, nombre)
    assert evento
    try:
        # Act / Assert
        with pytest.raises(BridgeCommandError):
            Win32JobObject(nombre)
    finally:
        k32.CloseHandle(evento)


def test_el_job_nativo_rechaza_un_nombre_con_formato_invalido() -> None:
    # Act / Assert: antes de tocar el SO.
    with pytest.raises(BridgeCommandError, match="job_object"):
        Win32JobObject("Global\\SkyClawVfs-" + "0" * 32)


# ---------------------------------------------------------------------------
# 4. El entrypoint REAL del worker
# ---------------------------------------------------------------------------


def test_el_entrypoint_real_del_worker_fuera_del_job_sale_con_72_y_sin_abrir_la_red(tmp_path: pathlib.Path) -> None:
    # Arrange: el Job pedido existe pero el worker NO está en él. El descriptor apunta a un puerto cerrado: si el worker
    # intentara conectar, fallaría con OTRO código (70); 72 prueba que se detuvo ANTES de abrir ningún socket.
    nombre = generar_nombre_de_job()
    job = Win32JobObject(nombre)
    home = tmp_path / "home"
    home.mkdir()
    entorno = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "PYTHONUNBUFFERED": "1"}
    try:
        # Act
        resultado = subprocess.run(  # noqa: S603 - argv fijo del test
            [
                sys.executable,
                "-m",
                "sky_claw.local.mo2.vfs_worker",
                "--manifest",
                str(tmp_path / "m.json"),
                "--descriptor",
                str(tmp_path / "d.json"),
                "--job-id",
                "job-x",
                "--job-object",
                nombre,
            ],
            env=entorno,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        # Assert
        assert resultado.returncode == 72, (resultado.stdout, resultado.stderr)
    finally:
        job.close()


def test_el_entrypoint_real_sin_job_object_sale_con_64(tmp_path: pathlib.Path) -> None:
    # Act
    resultado = subprocess.run(  # noqa: S603 - argv fijo del test
        [
            sys.executable,
            "-m",
            "sky_claw.local.mo2.vfs_worker",
            "--manifest",
            str(tmp_path / "m.json"),
            "--descriptor",
            str(tmp_path / "d.json"),
            "--job-id",
            "job-x",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    # Assert: un launch de un bridge viejo (sin el flag) no arranca.
    assert resultado.returncode == 64, (resultado.stdout, resultado.stderr)
