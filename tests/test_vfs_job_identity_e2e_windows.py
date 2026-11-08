"""La carrera ``startApplication`` → ``assign`` de punta a punta, con el worker REAL y el Job REAL (solo Windows).

Piezas reales: ``VfsExecutionBroker``, ``BridgeLaunchController`` (el de producción del plugin), ``Win32JobObject``
nombrado y el worker como proceso aparte —``python -m sky_claw.local.mo2.vfs_worker`` o, con
``SKYCLAW_FROZEN_EXE``, el ejecutable PyInstaller *onefile* CONGELADO—. El "MO2" es un ``organizer`` mínimo que
lanza el worker y devuelve el handle tras un retardo configurable: ese retardo ES la ventana entre
``startApplication`` y ``AssignProcessToJobObject`` que se quiere reproducir.

Dos caminos que deben comportarse distinto, y los dos se prueban:

* ``assign`` a tiempo → el worker efectivo se verifica DENTRO del Job, el job completa, y el nieto de la attestation
  y la herramienta corrieron contenidos;
* ``assign`` tarde → el worker efectivo NO está en el Job: falla cerrado (código 72) ANTES de conectar, de crear
  el nieto y de despachar nada; el broker no recibe resultado y no queda ningún proceso del árbol.

Con el ejecutable congelado, ``SKYCLAW_FROZEN_EXE`` ausente → SKIP explícito (= no verificado, nunca un PASS).
Una observación sin hallazgos NO prueba que la carrera no exista: el ``assign`` tardío la fuerza a propósito.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import pathlib
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import psutil
import pytest

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import (
    BridgeCommandError,
    BridgeLaunchController,
    bridge_error_event,
)
from sky_claw.local.mo2.vfs_broker import VfsBrokerError, VfsExecutionBroker
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION, VfsJobResult
from sky_claw.local.mo2.vfs_ipc import read_authenticated_message, write_authenticated_message
from tests.test_vfs_fence_de_supervision import _manifiesto
from tests.test_vfs_fence_e2e_proceso_real import _Observador, _OrganizerQueLanzaElWorkerReal

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Job Objects reales: solo Windows")

_EXE_CONGELADO = os.environ.get("SKYCLAW_FROZEN_EXE")
_CODIGO_DE_IDENTIDAD = 72
#: Las formas en que el worker lanza el nieto de la attestation: ``--probe-child`` (Python de desarrollo) y
#: ``--vfs-probe-child`` (ejecutable congelado). NO es subcadena una de la otra (``--vfs-probe-child`` no contiene
#: ``--probe-child``): un selector con una sola forma hace VACUA la aserción «no se creó el nieto» con el otro
#: ejecutable. Una ancla (``test_vfs_job_identity_anclas``) congela que esta tupla cubre todo lo que el worker usa.
BANDERAS_DEL_NIETO = ("--probe-child", "--vfs-probe-child")


@dataclass
class _Corrida:
    resultado: VfsJobResult | None = None
    error: BaseException | None = None
    codigo_del_worker: int | None = None
    pid_del_worker: int | None = None
    descendientes: dict[int, dict[str, Any]] = field(default_factory=dict)
    nietos_de_attestation: list[int] = field(default_factory=list)
    residuales: list[int] = field(default_factory=list)
    huerfanos: list[int] = field(default_factory=list)
    #: Al momento de cancelar: ¿el broker ya había aceptado el `hello` del worker (= identidad verificada)?
    worker_conectado_al_cancelar: bool | None = None


async def _correr(
    tmp_path: pathlib.Path,
    *,
    retardo_assign_s: float,
    congelado: pathlib.Path | None,
    cancelar_tras_nacer_s: float | None = None,
    cancelar_al_conectar: bool = False,
    retraso_del_ack_s: float = 0.0,
    arranque_lento_s: float = 0.0,
) -> _Corrida:
    """Un launch completo del broker con el worker real; devuelve lo observable, sin interpretarlo."""
    inicio = time.time()
    manifiesto = _manifiesto(tmp_path, timeout_seconds=90 if congelado else 30)
    job_id = manifiesto.job.job_id
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _ruta: None,
        fence_grace_seconds=0.5,
        connected_worker_exit_deadline_seconds=10.0,
    )
    await broker.start()
    descriptor = json.loads(broker.descriptor_path.read_text(encoding="utf-8"))
    secreto = base64.urlsafe_b64decode(descriptor["token"])
    home = tmp_path / "home"
    home.mkdir()
    python_directo = getattr(sys, "_base_executable", sys.executable)
    rutas_python = os.pathsep.join([str(pathlib.Path(__file__).resolve().parents[1]), *(p for p in sys.path if p)])
    entorno = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": rutas_python,
    }
    manifiesto_path = broker._jobs_dir / f"{job_id}.json"

    def armar_argv(nombre_del_job: str) -> list[str]:
        base = [
            "--manifest",
            str(manifiesto_path),
            "--descriptor",
            str(broker.descriptor_path),
            "--job-object",
            nombre_del_job,
            "--job-id",
            job_id,
        ]
        if congelado is not None:
            return [str(congelado), "--vfs-worker", *base]
        if arranque_lento_s > 0:
            # El envoltorio ES el proceso asignado al Job y duerme antes de ejecutar el worker real EN EL MISMO proceso
            # (``runpy``): así «el worker todavía no empezó a verificarse» es un estado construido, no una apuesta al reloj.
            envoltorio = (
                f"import time, runpy; time.sleep({arranque_lento_s}); "
                "runpy.run_module('sky_claw.local.mo2.vfs_worker', run_name='__main__')"
            )
            return [python_directo, "-c", envoltorio, *base]
        return [python_directo, "-m", "sky_claw.local.mo2.vfs_worker", *base]

    organizer = _OrganizerQueLanzaElWorkerReal(armar_argv, entorno, retardo_s=retardo_assign_s)
    bucle = asyncio.get_running_loop()
    cola: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def enviar_evento(evento: dict[str, object]) -> None:
        if evento.get("type") == "launch_ack" and retraso_del_ack_s > 0:
            # El `assign` ya ocurrió, pero el broker se entera tarde: el worker puede verificarse, conectar y quedar
            # esperando `supervised` (verificado pero NO autorizado).
            time.sleep(retraso_del_ack_s)
        bucle.call_soon_threadsafe(cola.put_nowait, dict(evento))

    worker_exe = congelado if congelado is not None else tmp_path / "worker.exe"
    if congelado is None:
        worker_exe.write_bytes(b"MZ")
    controlador = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker_exe,
        worker_prefix=("--vfs-worker",),
        descriptor_path=broker.descriptor_path,
        jobs_root=broker._jobs_dir,
        send_event=enviar_evento,
        process_waiter=lambda handle: organizer.waitForApplication(handle, False),
    )
    observador = _Observador(organizer, controlador, job_id)
    corrida = _Corrida()
    lector, escritor = await asyncio.open_connection(descriptor["host"], descriptor["port"])
    tareas: list[asyncio.Task[Any]] = []
    try:
        await write_authenticated_message(
            escritor,
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "hello",
                "role": "bridge",
                "instance_id": "portable-main",
                "session_id": descriptor["session_id"],
            },
            secreto,
        )
        await read_authenticated_message(lector, secreto)
        await broker.wait_until_ready(timeout=2)

        async def emisor() -> None:
            while True:
                await write_authenticated_message(escritor, await cola.get(), secreto)

        async def receptor() -> None:
            while True:
                mensaje = await read_authenticated_message(lector, secreto)
                if mensaje.get("type") == "launch_worker":
                    observador.iniciar()
                    try:
                        await asyncio.to_thread(controlador.launch, mensaje)
                    except BridgeCommandError as exc:
                        # Lo mismo que hace el plugin real ante un launch rechazado (plugin.py): `bridge_error`.
                        await cola.put(
                            bridge_error_event(command=mensaje.get("type"), job_id=mensaje.get("job_id"), exc=exc)
                        )
                elif mensaje.get("type") == "cancel":
                    await asyncio.to_thread(controlador.cancel, mensaje)

        tareas = [asyncio.create_task(emisor()), asyncio.create_task(receptor())]
        submit = asyncio.create_task(
            broker.submit(
                manifiesto.job,
                challenge=manifiesto.challenge,
                mo2_root=manifiesto.mo2_root,
                virtual_data_dir=manifiesto.virtual_data_dir,
            )
        )
        try:
            if cancelar_al_conectar:
                # Cancela cuando el broker YA aceptó el `hello` del worker (= identidad verificada). Es un evento, no un
                # plazo: con un plazo, un runner rápido o lento cambiaría el estado que el test dice probar.
                limite = time.monotonic() + 90.0
                while job_id not in broker._worker_by_job and time.monotonic() < limite:
                    await asyncio.sleep(0.02)
                corrida.worker_conectado_al_cancelar = job_id in broker._worker_by_job
                submit.cancel()
            elif cancelar_tras_nacer_s is not None:
                # Cancela una vez que el worker EXISTE (el bridge ya lo asignó y registró el job: de lo contrario el
                # `cancel` llegaría a un job desconocido) y mientras duerme en su envoltorio, antes de verificarse.
                limite = time.monotonic() + 60.0
                while organizer.proceso is None and time.monotonic() < limite:
                    await asyncio.sleep(0.01)
                await asyncio.sleep(cancelar_tras_nacer_s)
                corrida.worker_conectado_al_cancelar = job_id in broker._worker_by_job
                submit.cancel()
            corrida.resultado = await asyncio.wait_for(submit, timeout=150)
        except (VfsBrokerError, TimeoutError, asyncio.CancelledError) as exc:
            corrida.error = exc
        observador.detener()
        if organizer.proceso is not None:
            corrida.pid_del_worker = organizer.proceso.pid
            with contextlib.suppress(Exception):
                corrida.codigo_del_worker = organizer.proceso.wait(timeout=30)
        corrida.descendientes = dict(observador.descendientes)
        corrida.nietos_de_attestation = [
            pid
            for pid, ficha in corrida.descendientes.items()
            if any(bandera in (ficha["cmdline"] or "") for bandera in BANDERAS_DEL_NIETO)
        ]
        # Residuos: tras un plazo de gracia nada de lo observado (ni el worker) puede seguir vivo.
        pids = [*corrida.descendientes, *([corrida.pid_del_worker] if corrida.pid_del_worker else [])]
        limite = time.monotonic() + 8.0
        while time.monotonic() < limite and any(psutil.pid_exists(pid) for pid in pids):
            await asyncio.sleep(0.2)
        corrida.residuales = [pid for pid in pids if psutil.pid_exists(pid) and _es_el_mismo(pid, corrida)]
        # Huérfanos: un proceso cuyo padre ya murió no cuelga del árbol que sigue el observador. Se barre TODA la tabla
        # de procesos por la imagen propia (argv exacto), independiente de la relación padre→hijo.
        corrida.huerfanos = [pid for pid in _procesos_de_la_imagen_del_worker(inicio, congelado) if pid not in pids]
        return corrida
    finally:
        observador.detener()
        for tarea in tareas:
            tarea.cancel()
        for tarea in tareas:
            with contextlib.suppress(BaseException):
                await tarea
        with contextlib.suppress(Exception):
            controlador.stop()
        if organizer.proceso is not None:
            with contextlib.suppress(psutil.Error):
                for hijo in psutil.Process(organizer.proceso.pid).children(recursive=True):
                    hijo.kill()
                psutil.Process(organizer.proceso.pid).kill()
        escritor.close()
        with contextlib.suppress(Exception):
            await escritor.wait_closed()
        await broker.close()


def _procesos_de_la_imagen_del_worker(desde: float, congelado: pathlib.Path | None) -> list[int]:
    """Procesos vivos, nacidos desde ``desde``, que SON el worker o su nieto de attestation (por argv exacto o imagen)."""
    encontrados: list[int] = []
    for proceso in psutil.process_iter(["pid", "exe", "cmdline", "create_time"]):
        info = proceso.info
        if (info["create_time"] or 0) < desde - 1.0:
            continue
        argv = info["cmdline"] or []
        es_nuestro = (
            "--vfs-worker" in argv
            or any(bandera in argv for bandera in BANDERAS_DEL_NIETO)
            or "sky_claw.local.mo2.vfs_worker" in argv
            or (congelado is not None and (info["exe"] or "").casefold() == str(congelado).casefold())
        )
        if es_nuestro and info["pid"] != os.getpid():
            encontrados.append(int(info["pid"]))
    return encontrados


def _afirmar_todo_el_arbol_en_el_job(corrida: _Corrida) -> None:
    """Con el ``assign`` a tiempo NINGÚN descendiente observado puede estar fuera del Job del bridge.

    ``en_job`` es ``None`` cuando el proceso murió antes de poder medirse (los ``cmd.exe`` duran milisegundos): eso no se
    cuenta como «fuera». Lo que no puede aparecer es un ``False``: significaría un proceso nacido fuera del Job que el worker
    no detectó.
    """
    fuera = {pid: ficha["cmdline"] for pid, ficha in corrida.descendientes.items() if ficha["en_job"] is False}
    assert fuera == {}, f"descendientes FUERA del Job con el assign a tiempo: {fuera}"
    medidos = [ficha for ficha in corrida.descendientes.values() if ficha["en_job"] is True]
    assert medidos, "ningún descendiente se pudo medir: el test no demostraría la pertenencia al Job"


def _es_el_mismo(pid: int, corrida: _Corrida) -> bool:
    """El pid sigue siendo EL proceso observado (misma hora de creación), no uno que reutilizó el número."""
    ficha = corrida.descendientes.get(pid)
    if ficha is None:
        return True  # el worker raíz: su pid no se reutiliza dentro de la ventana de gracia
    try:
        return abs(psutil.Process(pid).create_time() - float(ficha["creado"])) < 1.0
    except psutil.Error:
        return False


# ---------------------------------------------------------------------------
# Python de desarrollo (worker real: ``python -m sky_claw.local.mo2.vfs_worker``)
# ---------------------------------------------------------------------------


async def test_con_el_assign_a_tiempo_el_worker_real_se_verifica_en_el_job_y_el_job_completa(
    tmp_path: pathlib.Path,
) -> None:
    # Arrange / Act: assign inmediato (el retardo es el de un Popen).
    corrida = await _correr(tmp_path, retardo_assign_s=0.0, congelado=None)
    # Assert
    assert corrida.error is None, corrida.error
    assert corrida.resultado is not None and corrida.resultado.success is True
    assert corrida.nietos_de_attestation, (
        "el worker verificado debía llegar a la attestation (nieto) y a la herramienta"
    )
    _afirmar_todo_el_arbol_en_el_job(corrida)
    assert corrida.residuales == []
    assert corrida.huerfanos == [], f"huérfanos de la imagen del worker: {corrida.huerfanos}"


async def test_con_un_assign_tardio_el_worker_real_falla_cerrado_sin_nieto_ni_herramienta_ni_residuos(
    tmp_path: pathlib.Path,
) -> None:
    # Arrange / Act: el handle llega 8 s tarde: el worker ya terminó de arrancar y se verificó FUERA del Job.
    corrida = await _correr(tmp_path, retardo_assign_s=8.0, congelado=None)
    # Assert 1: el broker NO recibió un resultado del job.
    assert corrida.resultado is None
    assert isinstance(corrida.error, VfsBrokerError), corrida.error
    # Assert 2: el worker salió por la verificación de identidad (72), antes de conectar.
    assert corrida.codigo_del_worker == _CODIGO_DE_IDENTIDAD
    # Assert 3: jamás se creó el nieto de la attestation (= jamás se despachó la herramienta).
    assert corrida.nietos_de_attestation == []
    # Assert 4: no quedó ningún proceso del árbol.
    assert corrida.residuales == [], f"procesos residuales: {corrida.residuales}"
    assert corrida.huerfanos == [], f"huérfanos de la imagen del worker: {corrida.huerfanos}"


# ---------------------------------------------------------------------------
# Ejecutable CONGELADO (PyInstaller onefile): lo que realmente se despliega
# ---------------------------------------------------------------------------


def _congelado() -> pathlib.Path:
    if not _EXE_CONGELADO:
        pytest.skip("SKYCLAW_FROZEN_EXE no definido: el ejecutable congelado NO se verificó")
    ruta = pathlib.Path(_EXE_CONGELADO).resolve()
    if not ruta.is_file():
        pytest.skip(f"SKYCLAW_FROZEN_EXE apunta a algo que no existe: {ruta}")
    return ruta


async def test_congelado_con_el_assign_a_tiempo_el_worker_efectivo_se_verifica_y_el_job_completa(
    tmp_path: pathlib.Path,
) -> None:
    # Arrange / Act: SkyClawApp.exe onefile = bootloader (el que el bridge asigna) + hijo Python (el que se verifica).
    corrida = await _correr(tmp_path, retardo_assign_s=0.0, congelado=_congelado())
    # Assert
    assert corrida.error is None, corrida.error
    assert corrida.resultado is not None and corrida.resultado.success is True, corrida.resultado
    assert corrida.nietos_de_attestation
    _afirmar_todo_el_arbol_en_el_job(corrida)
    assert corrida.residuales == []
    assert corrida.huerfanos == [], f"huérfanos de la imagen del worker: {corrida.huerfanos}"


async def test_congelado_con_un_assign_tardio_el_hijo_python_falla_cerrado_y_no_deja_residuos(
    tmp_path: pathlib.Path,
) -> None:
    # Arrange / Act: 12 s de retardo >> extracción del onefile: el hijo Python nace y se verifica ANTES del assign.
    corrida = await _correr(tmp_path, retardo_assign_s=12.0, congelado=_congelado())
    # Assert 1
    assert corrida.resultado is None
    assert isinstance(corrida.error, VfsBrokerError), corrida.error
    # Assert 2: jamás se llegó a la attestation ni a la herramienta.
    assert corrida.nietos_de_attestation == []
    # Assert 3: el árbol completo (bootloader, hijo Python y sus auxiliares) terminó: nada vivo del árbol observado.
    assert corrida.residuales == [], f"procesos residuales: {corrida.residuales}"
    assert corrida.huerfanos == [], f"huérfanos de la imagen del worker: {corrida.huerfanos}"


async def test_cancelar_antes_de_la_verificacion_termina_al_worker_con_el_job_y_no_deja_residuos(
    tmp_path: pathlib.Path,
) -> None:
    # Arrange: el proceso asignado al Job duerme 8 s ANTES de iniciar el worker real (``arranque_lento_s``).
    # Act: 1 s después de nacer —assign hecho, job registrado en el bridge— se cancela: la verificación NO pudo haber ocurrido.
    corrida = await _correr(
        tmp_path, retardo_assign_s=0.0, congelado=None, arranque_lento_s=8.0, cancelar_tras_nacer_s=1.0
    )
    # Assert 0: el estado de partida es el que se quería probar.
    assert corrida.worker_conectado_al_cancelar is False, "el worker no debía haberse conectado todavía"
    # Assert 1: no hubo resultado; la cancelación llegó al caller.
    assert corrida.resultado is None
    assert isinstance(corrida.error, asyncio.CancelledError), corrida.error
    # Assert 2: TerminateJobObject mató al worker (código 1) ANTES de que llegara a verificarse (72) o a trabajar.
    assert corrida.codigo_del_worker == 1, corrida.codigo_del_worker
    # Assert 3: nada de la attestation ni de la herramienta, y ningún proceso residual.
    assert corrida.nietos_de_attestation == []
    assert corrida.residuales == [], f"procesos residuales: {corrida.residuales}"
    assert corrida.huerfanos == [], f"huérfanos de la imagen del worker: {corrida.huerfanos}"


async def test_cancelar_despues_de_verificarse_y_antes_de_ser_autorizado_termina_al_worker_sin_herramienta(
    tmp_path: pathlib.Path,
) -> None:
    # Arrange: el ack llega 20 s tarde → el worker se verifica, conecta y queda esperando `supervised`.
    # Act: se cancela en cuanto el broker acepta su `hello` (evento), con el worker VERIFICADO pero NO autorizado.
    corrida = await _correr(
        tmp_path, retardo_assign_s=0.0, congelado=None, cancelar_al_conectar=True, retraso_del_ack_s=20.0
    )
    # Assert 1: el estado de partida es el que se quería probar (identidad aceptada por el broker, sin autorización).
    assert corrida.worker_conectado_al_cancelar is True, (
        "el worker debía haberse verificado y conectado antes de cancelar"
    )
    # Assert 2: la cancelación llegó al caller y no hubo resultado.
    assert corrida.resultado is None
    assert isinstance(corrida.error, asyncio.CancelledError), corrida.error
    # Assert 3: murió por la cancelación y jamás recibió `supervised`: ni nieto ni herramienta ni residuos. Dos desenlaces
    # son válidos y MEDIDOS en Windows: 2 = el worker vio el `cancel` mientras esperaba `supervised` y salió solo
    # (cancelación cooperativa); 1 = `TerminateJobObject` llegó primero. Cualquier otro código sería otra historia.
    assert corrida.codigo_del_worker in (1, 2), corrida.codigo_del_worker
    assert corrida.nietos_de_attestation == []
    assert corrida.residuales == [], f"procesos residuales: {corrida.residuales}"
    assert corrida.huerfanos == [], f"huérfanos de la imagen del worker: {corrida.huerfanos}"
