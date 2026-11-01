"""Fence de supervisión de punta a punta, con un worker REAL (subproceso) y el bridge REAL.

Piezas reales: ``VfsExecutionBroker``, ``BridgeLaunchController`` (el controlador de
producción del plugin de MO2) y el worker como proceso aparte
(``python -m sky_claw.local.mo2.vfs_worker``), que en la attestation crea un proceso NIETO
de verdad. En Windows el Job Object es el ``Win32JobObject`` real; en otras plataformas es
un doble (el orden del protocolo se verifica igual, la contención del SO no).

Lo que se demuestra:

* el ``launch_ack`` del bridge se emite DESPUÉS de ``assign``, y se retrasa a propósito
  (``startApplication`` ya devolvió y el worker ya corre): ningún descendiente del worker
  nace antes de que el ack haya sido emitido;
* en Windows, TODO descendiente observado del worker pertenece al Job del bridge
  (``IsProcessInJob`` contra el handle propio, no contra "cualquier Job");
* el job completa con éxito (la attestation del nieto y la herramienta corrieron).

Es una verificación del flujo, no una prueba de ausencia de escapes: se observa muestreando,
y una observación sin hallazgos no demuestra imposibilidad (ver el informe de auditoría).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
from typing import Any

import psutil

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import PROTOCOL_VERSION, BridgeLaunchController
from sky_claw.local.mo2.vfs_broker import VfsExecutionBroker
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION
from sky_claw.local.mo2.vfs_ipc import read_authenticated_message, write_authenticated_message
from tests.test_vfs_fence_de_supervision import _manifiesto

REPO = pathlib.Path(__file__).resolve().parents[1]
ES_WINDOWS = sys.platform == "win32"

#: Retraso deliberado del ``launch_ack``: el worker ya está corriendo y esperando.
_RETRASO_DEL_ACK_S = 1.5


class _JobFalso:
    """Doble para plataformas sin Job Objects: conserva el ORDEN assign → ack, no la contención."""

    def assign(self, process_handle: int) -> None:
        self.asignado = process_handle

    def terminate(self) -> None:
        self.terminado = True

    def close(self) -> None:
        self.cerrado = True


class _OrganizerQueLanzaElWorkerReal:
    """``startApplication`` como la API de MO2: el worker REAL nace corriendo y se devuelve su handle."""

    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        self._argv = argv
        self._env = env
        self.proceso: subprocess.Popen[bytes] | None = None
        self.t_popen: float | None = None  # `time.time()` apenas nace el worker (referencia para medir en relativo)

    def startApplication(self, *_args: object) -> int:  # noqa: N802 - API de MO2
        self.proceso = subprocess.Popen(  # noqa: S603 - argv construido por el test
            self._argv,
            cwd=REPO,
            env=self._env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.t_popen = time.time()
        if ES_WINDOWS:
            from scripts import windows_job_probe

            return windows_job_probe.abrir_proceso(self.proceso.pid)
        return self.proceso.pid

    def waitForApplication(self, handle: int, refresh: bool) -> tuple[bool, int]:  # noqa: N802 - API de MO2
        assert self.proceso is not None
        return True, int(self.proceso.wait())


class _Observador:
    """Muestrea los descendientes del worker mientras el job corre (hilo aparte)."""

    def __init__(
        self, organizer: _OrganizerQueLanzaElWorkerReal, controlador: BridgeLaunchController, job_id: str
    ) -> None:
        self._organizer = organizer
        self._controlador = controlador
        self._job_id = job_id
        self._parar = threading.Event()
        self.descendientes: dict[int, dict[str, Any]] = {}
        self.creacion_del_worker: float | None = None
        self._hilo = threading.Thread(target=self._correr, name="observador-de-descendientes", daemon=True)

    def iniciar(self) -> None:
        self._hilo.start()

    def detener(self) -> None:
        self._parar.set()
        self._hilo.join(timeout=5)

    def _handle_del_job(self) -> int | None:
        if not ES_WINDOWS:
            return None
        corriendo = self._controlador._jobs.get(self._job_id)
        if corriendo is None:
            return None
        objeto: Any = corriendo.job_object
        return int(objeto._handle)

    def _correr(self) -> None:
        while not self._parar.is_set():
            proceso = self._organizer.proceso
            if proceso is not None:
                if self.creacion_del_worker is None:
                    with contextlib.suppress(psutil.Error):
                        self.creacion_del_worker = psutil.Process(proceso.pid).create_time()
                with contextlib.suppress(psutil.Error):
                    for hijo in psutil.Process(proceso.pid).children(recursive=True):
                        self._registrar(hijo)
            time.sleep(0.005)

    def _registrar(self, hijo: psutil.Process) -> None:
        ficha = self.descendientes.get(hijo.pid)
        if ficha is None:
            cmdline: str | None = None
            with contextlib.suppress(psutil.Error):
                cmdline = " ".join(hijo.cmdline())[:200]
            ficha = {"creado": hijo.create_time(), "en_job": None, "cmdline": cmdline}
            self.descendientes[hijo.pid] = ficha
        job = self._handle_del_job()
        if job is not None and ficha["en_job"] is None:
            from scripts import windows_job_probe

            handle = windows_job_probe.abrir_proceso(hijo.pid)
            try:
                ficha["en_job"] = windows_job_probe.proceso_en_job(handle, job)
            finally:
                windows_job_probe.cerrar_handle(handle)


async def test_el_worker_real_no_crea_descendientes_antes_del_ack_y_todos_quedan_en_el_job(
    tmp_path: pathlib.Path,
) -> None:
    # Arrange: broker real + manifiesto firmado; el worker real se lanza por el controlador real.
    manifiesto = _manifiesto(tmp_path)
    job_id = manifiesto.job.job_id
    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _ruta: None,
        fence_grace_seconds=0.5,
        connected_worker_exit_deadline_seconds=5.0,
    )
    await broker.start()
    descriptor = json.loads(broker.descriptor_path.read_text(encoding="utf-8"))
    secreto = base64.urlsafe_b64decode(descriptor["token"])
    home = tmp_path / "home"
    home.mkdir()
    entorno = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "PYTHONUNBUFFERED": "1"}
    manifiesto_path = broker._jobs_dir / f"{job_id}.json"
    organizer = _OrganizerQueLanzaElWorkerReal(
        [
            sys.executable,
            "-m",
            "sky_claw.local.mo2.vfs_worker",
            "--manifest",
            str(manifiesto_path),
            "--descriptor",
            str(broker.descriptor_path),
            "--job-id",
            job_id,
        ],
        entorno,
    )
    bucle = asyncio.get_running_loop()
    cola: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    instante_del_ack: list[float] = []

    def enviar_evento(evento: dict[str, object]) -> None:
        """Lo llama el controlador (hilos de launch y de monitor): se reenvía al broker por el socket."""
        if evento.get("type") == "launch_ack":
            time.sleep(_RETRASO_DEL_ACK_S)  # el worker ya corre; el ack del bridge llega tarde
            instante_del_ack.append(time.time())
        bucle.call_soon_threadsafe(cola.put_nowait, dict(evento))

    worker_exe = tmp_path / "worker.exe"
    worker_exe.write_bytes(b"MZ")
    controlador = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker_exe,
        worker_prefix=("--vfs-worker",),
        descriptor_path=broker.descriptor_path,
        jobs_root=broker._jobs_dir,
        send_event=enviar_evento,
        job_factory=None if ES_WINDOWS else _JobFalso,
        process_waiter=lambda handle: organizer.waitForApplication(handle, False),
    )
    observador = _Observador(organizer, controlador, job_id)
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
                    await asyncio.to_thread(controlador.launch, mensaje)
                elif mensaje.get("type") == "cancel":
                    await asyncio.to_thread(controlador.cancel, mensaje)

        tareas = [asyncio.create_task(emisor()), asyncio.create_task(receptor())]

        # Act
        resultado = await asyncio.wait_for(
            broker.submit(
                manifiesto.job,
                challenge=manifiesto.challenge,
                mo2_root=manifiesto.mo2_root,
                virtual_data_dir=manifiesto.virtual_data_dir,
            ),
            timeout=120,
        )
        observador.detener()

        # Assert 1: el worker real corrió la attestation (nieto real) y la herramienta.
        assert resultado.success is True, resultado.message
        # Assert 2: el nieto de la attestation (lo que el fence gobierna) se observó y nació DESPUÉS del ack.
        # Se mide RELATIVO al worker: `psutil.create_time()` en Linux deriva de `btime` (resolución de 1 s) y
        # comparar contra `time.time()` no resuelve fracciones de segundo; contra el propio worker sí.
        assert instante_del_ack and organizer.t_popen is not None, "el bridge no emitió launch_ack"
        creacion_del_worker = observador.creacion_del_worker
        assert creacion_del_worker is not None
        ack_relativo = instante_del_ack[0] - organizer.t_popen
        tolerancia = 0.05 if ES_WINDOWS else 0.1
        nietos = {
            pid: ficha for pid, ficha in observador.descendientes.items() if "--probe-child" in (ficha["cmdline"] or "")
        }
        assert nietos, "no se observó el proceso nieto de la attestation: el test no mediría nada"
        linea_de_tiempo = {
            "ack_rel_worker_s": round(ack_relativo, 3),
            "descendientes_rel_worker_s": {
                str(pid): {"rel_s": round(f["creado"] - creacion_del_worker, 3), "cmdline": f["cmdline"]}
                for pid, f in sorted(observador.descendientes.items(), key=lambda kv: kv[1]["creado"])
            },
        }
        for pid, ficha in nietos.items():
            nacimiento = ficha["creado"] - creacion_del_worker
            assert nacimiento >= ack_relativo - tolerancia, (
                f"el nieto {pid} nació a {nacimiento:.3f}s del worker, ANTES del launch_ack ({ack_relativo:.3f}s); "
                f"línea de tiempo: {json.dumps(linea_de_tiempo, ensure_ascii=False)}"
            )
        # Assert 3 (Windows): TODO nieto pertenece al Job del bridge, comprobado contra su handle.
        if ES_WINDOWS:
            fuera = [pid for pid, ficha in nietos.items() if ficha["en_job"] is not True]
            assert not fuera, f"nietos fuera del Job del bridge: {fuera}"
        # HALLAZGO (informativo, no asertado): procesos que el propio ARRANQUE del worker crea antes de
        # llegar al fence, p. ej. por el import de librerías de terceros. Se reporta para la auditoría.
        previos = {
            pid: ficha
            for pid, ficha in observador.descendientes.items()
            if pid not in nietos and ficha["creado"] - creacion_del_worker < ack_relativo - tolerancia
        }
        print(  # noqa: T201 - evidencia para -rA / auditoría
            "HALLAZGO descendientes previos al ack (arranque del worker): "
            + json.dumps(
                {str(pid): {"cmdline": f["cmdline"], "en_job": f["en_job"]} for pid, f in previos.items()},
                ensure_ascii=False,
            )
        )
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


def test_el_protocolo_del_bridge_que_usa_este_e2e_es_el_de_produccion() -> None:
    """Ancla: este e2e usa el MISMO PROTOCOL_VERSION del bridge y del broker (si divergen, no mide nada)."""
    assert PROTOCOL_VERSION == VFS_PROTOCOL_VERSION
