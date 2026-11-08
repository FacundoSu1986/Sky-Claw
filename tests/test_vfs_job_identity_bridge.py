"""Identidad del Job en el bridge (P2): el Job se crea CON el nombre pedido, antes de lanzar, o no se lanza nada.

El bridge corre dentro de MO2 y no puede importar ``sky_claw``: lleva su propia copia del formato del nombre.
Estos tests fijan lo que el bridge hace con el ``job_object`` que le manda el broker:

* lo valida con un formato cerrado (al SO no llega un string arbitrario) y lo exige: sin él no hay launch;
* crea el Job con ESE nombre ANTES de ``startApplication``: si el nombre ya existía (squatting, reutilización)
  la creación falla y no nace ningún proceso;
* se lo pasa al worker por argv y lo confirma en el ``launch_ack``;
* un bridge viejo rechaza el campo (``campos no permitidos``) y por eso jamás puede confirmar un launch P2.

La creación real de un Job nombrado, su ACL y ``ERROR_ALREADY_EXISTS`` se prueban en
``test_vfs_job_identity_windows.py``: un doble no reemplaza esa validación nativa.
"""

from __future__ import annotations

import pathlib
import sys
import threading

import pytest

from sky_claw.local.mo2 import vfs_job_identity
from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge import runtime as puente
from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import (
    PROTOCOL_VERSION,
    BridgeCommandError,
    BridgeLaunchController,
    build_worker_arguments,
    validate_launch_request,
)

NOMBRE = "Local\\SkyClawVfs-" + "ab" * 16


class _Organizer:
    """``startApplication`` de MO2: registra el orden respecto de la creación del Job."""

    def __init__(self, bitacora: list[str]) -> None:
        self.bitacora = bitacora
        self.liberar = threading.Event()

    def startApplication(self, *args: object) -> int:  # noqa: N802 - API de MO2
        self.bitacora.append("startApplication")
        self.argumentos = args
        return 123

    def waitForApplication(self, handle: int, refresh: bool) -> tuple[bool, int]:  # noqa: N802 - API de MO2
        self.liberar.wait(timeout=1)
        return True, 0


class _Job:
    def __init__(self, nombre: str, bitacora: list[str]) -> None:
        self.nombre = nombre
        bitacora.append(f"job:{nombre}")
        self.asignados: list[int] = []
        self.cerrado = False

    def assign(self, process_handle: int) -> None:
        self.asignados.append(process_handle)

    def terminate(self) -> None:
        pass

    def adopt_descendants(self, process_handle: int) -> int:
        return 0

    def active_processes(self) -> int:
        return 0

    def close(self) -> None:
        self.cerrado = True


def _mensaje(manifiesto: pathlib.Path, **cambios: object) -> dict[str, object]:
    mensaje: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "type": "launch_worker",
        "job_id": "job-1",
        "profile": "Default",
        "manifest_path": str(manifiesto),
        "overwrite_mod": None,
        "job_object": NOMBRE,
    }
    mensaje.update(cambios)
    return {clave: valor for clave, valor in mensaje.items() if valor is not _OMITIR}


_OMITIR = object()


def _escenario(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifiesto = jobs / "job-1.json"
    manifiesto.write_text("{}", encoding="utf-8")
    worker = tmp_path / "SkyClawApp.exe"
    worker.write_bytes(b"exe")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    return jobs, manifiesto, worker


def _controlador(
    tmp_path: pathlib.Path, bitacora: list[str], eventos: list[dict[str, object]], *, fabrica: object = None
) -> tuple[BridgeLaunchController, _Organizer, pathlib.Path]:
    jobs, manifiesto, worker = _escenario(tmp_path)
    organizer = _Organizer(bitacora)
    controlador = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=tmp_path / "descriptor.json",
        jobs_root=jobs,
        send_event=eventos.append,
        job_factory=fabrica or (lambda nombre: _Job(nombre, bitacora)),  # type: ignore[arg-type]
        process_waiter=lambda handle: organizer.waitForApplication(handle, False),
    )
    return controlador, organizer, manifiesto


# ---------------------------------------------------------------------------
# Validación del request
# ---------------------------------------------------------------------------


def test_el_request_conserva_el_job_object_validado(tmp_path: pathlib.Path) -> None:
    # Arrange
    jobs, manifiesto, _ = _escenario(tmp_path)
    # Act
    request = validate_launch_request(_mensaje(manifiesto), jobs_root=jobs)
    # Assert
    assert request.job_object == NOMBRE


@pytest.mark.parametrize(
    "malo",
    [
        pytest.param(_OMITIR, id="ausente"),
        pytest.param(None, id="nulo"),
        pytest.param("", id="vacio"),
        pytest.param(7, id="no_es_string"),
        pytest.param("Global\\SkyClawVfs-" + "ab" * 16, id="otro_espacio_de_nombres"),
        pytest.param("Local\\SkyClawVfs-" + "AB" * 16, id="mayusculas"),
        pytest.param("Local\\SkyClawVfs-" + "ab" * 16 + "\n", id="salto_de_linea_final"),
        pytest.param("Local\\SkyClawVfs-" + "ab" * 15, id="corto"),
        pytest.param("Local\\Otro-" + "ab" * 16, id="otro_prefijo"),
        pytest.param("Local\\SkyClawVfs-" + "ab" * 16 + "\\..\\x", id="intento_de_salirse"),
    ],
)
def test_un_job_object_ausente_o_mal_formado_se_rechaza(tmp_path: pathlib.Path, malo: object) -> None:
    # Arrange
    jobs, manifiesto, _ = _escenario(tmp_path)
    # Act / Assert
    with pytest.raises(BridgeCommandError, match="job_object"):
        validate_launch_request(_mensaje(manifiesto, job_object=malo), jobs_root=jobs)


def test_el_formato_del_nombre_del_bridge_es_el_mismo_que_el_del_broker_y_el_worker() -> None:
    """El plugin no puede importar ``sky_claw``: su copia del patrón se ancla por IGUALDAD literal."""
    assert puente.JOB_OBJECT_NAME_PATTERN == vfs_job_identity.PATRON_DEL_NOMBRE_DE_JOB


def test_los_argumentos_del_worker_llevan_el_job_object_antes_del_job_id(tmp_path: pathlib.Path) -> None:
    # Arrange
    jobs, manifiesto, _ = _escenario(tmp_path)
    request = validate_launch_request(_mensaje(manifiesto), jobs_root=jobs)
    # Act
    argumentos = build_worker_arguments(
        fixed_prefix=("--vfs-worker",), request=request, descriptor_path=tmp_path / "descriptor.json"
    )
    # Assert: se conserva el final `--job-id <id>` (otros tests y el worker lo asumen).
    assert argumentos[-4:] == ["--job-object", NOMBRE, "--job-id", "job-1"]
    assert argumentos.count("--job-object") == 1


# ---------------------------------------------------------------------------
# El controlador: Job con nombre primero, proceso después
# ---------------------------------------------------------------------------


def test_el_job_se_crea_con_el_nombre_pedido_antes_de_lanzar_el_worker(tmp_path: pathlib.Path) -> None:
    # Arrange
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, organizer, manifiesto = _controlador(tmp_path, bitacora, eventos)
    # Act
    controlador.launch(_mensaje(manifiesto))
    # Assert: el Job existe (con SU nombre) antes de que nazca ningún proceso.
    assert bitacora == [f"job:{NOMBRE}", "startApplication"]
    organizer.liberar.set()
    controlador.wait_for_monitors(timeout=1)


def test_el_launch_ack_nombra_el_job_que_se_creo(tmp_path: pathlib.Path) -> None:
    # Arrange
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, organizer, manifiesto = _controlador(tmp_path, bitacora, eventos)
    # Act
    controlador.launch(_mensaje(manifiesto))
    # Assert: el broker compara este nombre con el que pidió; el bridge no inventa ni omite la identidad.
    ack = eventos[0]
    assert ack["type"] == "launch_ack"
    assert ack["job_object"] == NOMBRE
    organizer.liberar.set()
    controlador.wait_for_monitors(timeout=1)


def test_si_no_se_puede_crear_el_job_con_ese_nombre_no_nace_ningun_proceso(tmp_path: pathlib.Path) -> None:
    # Arrange: el nombre ya existe (squatting o reutilización) → la creación falla.
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []

    def fabrica_que_colisiona(nombre: str) -> _Job:
        raise BridgeCommandError(f"el Job {nombre} ya existía: no se adopta")

    controlador, organizer, manifiesto = _controlador(tmp_path, bitacora, eventos, fabrica=fabrica_que_colisiona)
    # Act / Assert
    with pytest.raises(BridgeCommandError, match="ya existía"):
        controlador.launch(_mensaje(manifiesto))
    assert bitacora == [], "no debe llamarse a startApplication: nada que contener ni que dejar suelto"
    assert eventos == [], "sin Job no hay launch_ack"
    assert controlador._jobs == {}


@pytest.mark.parametrize("malo", [_OMITIR, None, "", "Global\\SkyClawVfs-" + "ab" * 16])
def test_un_launch_sin_job_object_valido_no_crea_job_ni_lanza(tmp_path: pathlib.Path, malo: object) -> None:
    # Arrange
    bitacora: list[str] = []
    eventos: list[dict[str, object]] = []
    controlador, _organizer, manifiesto = _controlador(tmp_path, bitacora, eventos)
    # Act / Assert
    with pytest.raises(BridgeCommandError):
        controlador.launch(_mensaje(manifiesto, job_object=malo))
    assert bitacora == [] and eventos == []


def test_el_worker_recibe_el_job_object_por_argv(tmp_path: pathlib.Path) -> None:
    # Arrange
    bitacora: list[str] = []
    controlador, organizer, manifiesto = _controlador(tmp_path, bitacora, [])
    # Act
    controlador.launch(_mensaje(manifiesto))
    # Assert: el argv que MO2 recibe (fragmentos ya serializados) lleva `--job-object <nombre>`.
    _ejecutable, argumentos, *_resto = organizer.argumentos
    assert "--job-object" in argumentos
    assert argumentos[argumentos.index("--job-object") + 1] == NOMBRE
    organizer.liberar.set()
    controlador.wait_for_monitors(timeout=1)


@pytest.mark.skipif(sys.platform == "win32", reason="en Windows el Job real se crea")
def test_fuera_de_windows_el_job_nativo_falla_cerrado_con_nombre() -> None:
    # Act / Assert: el controlador de producción usa Win32JobObject(nombre); sin Windows no hay contención posible.
    with pytest.raises(BridgeCommandError, match="Windows"):
        puente.Win32JobObject(NOMBRE)
