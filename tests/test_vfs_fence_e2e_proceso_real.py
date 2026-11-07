"""Fence de supervisión de punta a punta, con un worker REAL (subproceso) y el bridge REAL.

Piezas reales: ``VfsExecutionBroker``, ``BridgeLaunchController`` (el controlador de
producción del plugin de MO2) y el worker como proceso aparte
(``python -m sky_claw.local.mo2.vfs_worker``), que en la attestation crea un proceso NIETO
de verdad. En Windows el Job Object es el ``Win32JobObject`` real; en otras plataformas es
un doble (el orden del protocolo se verifica igual, la contención del SO no).

Lo que se demuestra:

* el ``launch_ack`` del bridge se emite DESPUÉS de ``assign``, y se retrasa a propósito
  (``startApplication`` ya devolvió y el worker ya corre): el nieto de la attestation
  (``--probe-child``) no nace antes de que el ack haya sido emitido. Lo que el intérprete
  crea al arrancar (anterior al ``hello``) no lo cubre el fence y se informa como HALLAZGO;
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
from collections.abc import Callable
from typing import Any, cast

import psutil
import pytest

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import PROTOCOL_VERSION, BridgeLaunchController
from sky_claw.local.mo2.vfs_broker import VfsExecutionBroker
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION
from sky_claw.local.mo2.vfs_ipc import read_authenticated_message, write_authenticated_message
from tests.test_vfs_fence_de_supervision import _manifiesto

REPO = pathlib.Path(__file__).resolve().parents[1]
ES_WINDOWS = sys.platform == "win32"

#: Lanzador del worker en plataformas sin Job Objects (ver ``armar_argv``): reemplaza únicamente la verificación.
_LANZADOR_SIN_WINDOWS = (
    "import sys;"
    "from sky_claw.local.mo2 import vfs_worker as w;"
    "from sky_claw.local.mo2.vfs_job_identity import PruebaDeJob;"
    "w.verificar_identidad_del_job = lambda n: PruebaDeJob(nombre=n, pid=0, procesos_totales=0, procesos_activos=0);"
    "raise SystemExit(w.worker_main(sys.argv[1:]))"
)

#: Retraso deliberado del ``launch_ack``: el worker ya está corriendo y esperando.
_RETRASO_DEL_ACK_S = 1.5


class _JobFalso:
    """Doble para plataformas sin Job Objects: conserva el ORDEN assign → ack, no la contención."""

    def __init__(self, nombre: str) -> None:
        self.nombre = nombre

    def assign(self, process_handle: int) -> None:
        self.asignado = process_handle

    def terminate(self) -> None:
        self.terminado = True

    def close(self) -> None:
        self.cerrado = True


class _OrganizerQueLanzaElWorkerReal:
    """``startApplication`` como la API de MO2: el worker REAL nace corriendo y se devuelve su handle."""

    def __init__(self, armar_argv: Callable[[str], list[str]], env: dict[str, str], *, retardo_s: float = 0.0) -> None:
        self._armar_argv = armar_argv
        self._env = env
        #: Tiempo que `startApplication` tarda en DEVOLVER el handle tras crear el proceso (= retardo del assign).
        self._retardo_s = retardo_s
        self.proceso: subprocess.Popen[bytes] | None = None
        self.t_popen: float | None = None  # `time.time()` apenas nace el worker (referencia para medir en relativo)

    def startApplication(self, _ejecutable: str, argumentos: list[str], *_resto: object) -> int:  # noqa: N802 - API de MO2
        # El nombre del Job llega por el camino REAL: el broker lo pide en `launch_worker`, el bridge lo pone en el argv
        # que MO2 recibe, y de ahí lo toma el worker. El test no lo conoce de antemano.
        nombre = argumentos[argumentos.index("--job-object") + 1]
        self.proceso = subprocess.Popen(  # noqa: S603 - argv construido por el test
            self._armar_argv(nombre),
            cwd=REPO,
            env=self._env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.t_popen = time.time()
        if self._retardo_s > 0:
            time.sleep(self._retardo_s)
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

            try:
                handle = windows_job_probe.abrir_proceso(hijo.pid)
            except windows_job_probe.JobObjectError as exc:
                # El hijo pudo terminar entre `children()` y `OpenProcess` (GetLastError=87): los `cmd.exe` del
                # arranque del worker viven milisegundos. Antes esta excepción mataba el hilo y el observador dejaba
                # de muestrear, así que el nieto de la attestation (que nace después) nunca se registraba. Se anota
                # que no se pudo medir y se sigue; el assert 3 distingue «no medido» de «fuera del Job».
                ficha["sin_medir"] = str(exc)
                return
            ficha.pop("sin_medir", None)
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
    python_directo = getattr(sys, "_base_executable", sys.executable)
    rutas_python = os.pathsep.join([str(REPO), *(p for p in sys.path if p)])
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
        if ES_WINDOWS:
            # Worker de producción tal cual: la verificación de identidad contra el Job REAL del bridge.
            return [python_directo, "-m", "sky_claw", "--vfs-worker", *base]
        # Sin Windows no hay Job Object que verificar y la verificación real falla cerrado (a propósito). El subproceso
        # sustituye SOLO esa función por una prueba; el resto del worker (hello, fence, attestation, herramienta) es real.
        return [python_directo, "-c", _LANZADOR_SIN_WINDOWS, *base]

    organizer = _OrganizerQueLanzaElWorkerReal(armar_argv, entorno)
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
            fuera = [pid for pid, ficha in nietos.items() if ficha["en_job"] is False]
            sin_medir = [pid for pid, ficha in nietos.items() if ficha["en_job"] is None]
            assert not fuera, f"nietos fuera del Job del bridge: {fuera}"
            assert not sin_medir, (
                f"nietos que terminaron antes de poder medirse ({sin_medir}): el test no mediría su pertenencia al Job"
            )
        # Assert 4: el propio ARRANQUE del worker no crea subprocesos antes de llegar al fence
        # (antes `sky_claw/__init__.py` y `sky_claw/app/security/__init__.py` importaban ansiosamente
        # `config` → `keyring` → `platform.win32_ver()`, disparando `cmd.exe /c ver` antes del fence).
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
        assert not previos, (
            f"el worker creó subprocesos durante el arranque antes del launch_ack: "
            f"{json.dumps(previos, ensure_ascii=False)}"
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


class _HijoFalso:
    """Doble mínimo de ``psutil.Process`` para ``_Observador._registrar``."""

    def __init__(self, pid: int, cmdline: list[str]) -> None:
        self.pid = pid
        self._cmdline = cmdline

    def cmdline(self) -> list[str]:
        return self._cmdline

    def create_time(self) -> float:
        return 1000.0 + self.pid / 1_000_000


def test_el_observador_sobrevive_a_un_hijo_que_termino_antes_de_poder_abrirse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un hijo de vida corta puede morir entre ``children()`` y ``OpenProcess``; el observador sigue muestreando.

    En el CI de #856 el ``OpenProcess`` de uno de esos hijos (los ``cmd.exe`` del arranque del worker)
    falló con ``GetLastError=87`` y la excepción mató el hilo: dejó de muestrear y el nieto de la
    attestation, que nace DESPUÉS, no se registró («el test no mediría nada»). El observador tiene que
    anotar lo que no pudo medir y seguir con el resto.
    """
    from scripts import windows_job_probe

    # Arrange: el pid 2952 ya no existe al abrirlo; el 3000 (el nieto) sí, y está en el Job.
    def abrir_proceso(pid: int) -> int:
        if pid == 2952:
            raise windows_job_probe.JobObjectError(87, f"OpenProcess({pid}) falló (GetLastError=87)")
        return 99

    monkeypatch.setattr(windows_job_probe, "abrir_proceso", abrir_proceso)
    monkeypatch.setattr(windows_job_probe, "proceso_en_job", lambda _handle, _job: True)
    monkeypatch.setattr(windows_job_probe, "cerrar_handle", lambda _handle: None)
    observador = _Observador(cast(Any, None), cast(Any, None), "job-1")
    monkeypatch.setattr(observador, "_handle_del_job", lambda: 1234)
    efimero = cast(psutil.Process, _HijoFalso(2952, ["cmd.exe", "/c", "ver"]))
    nieto = cast(psutil.Process, _HijoFalso(3000, ["python.exe", "--probe-child"]))

    # Act: no debe lanzar, y el hijo siguiente se registra y se mide.
    observador._registrar(efimero)
    observador._registrar(nieto)

    # Assert
    assert observador.descendientes[2952]["en_job"] is None, "un hijo que no se pudo abrir no se da por medido"
    assert "GetLastError=87" in observador.descendientes[2952]["sin_medir"]
    assert observador.descendientes[3000]["en_job"] is True
    assert "sin_medir" not in observador.descendientes[3000]


def test_el_protocolo_del_bridge_que_usa_este_e2e_es_el_de_produccion() -> None:
    """Ancla: este e2e usa el MISMO PROTOCOL_VERSION del bridge y del broker (si divergen, no mide nada)."""
    assert PROTOCOL_VERSION == VFS_PROTOCOL_VERSION


@pytest.mark.parametrize("flag_entrada", [None, "--vfs-worker", "--vfs-probe-child"])
def test_importar_vfs_worker_no_carga_config_ni_keyring_ni_lanza_subprocesos(
    flag_entrada: str | None,
) -> None:
    """Tanto ``vfs_worker`` directo como ``sky_claw.__main__`` con flags VFS evitan ``config``/``keyring``."""
    python_directo = getattr(sys, "_base_executable", sys.executable)
    rutas_python = os.pathsep.join([str(REPO), *(p for p in sys.path if p)])
    accion = (
        "import sky_claw.local.mo2.vfs_worker"
        if flag_entrada is None
        else (
            "import sky_claw.local.mo2.vfs_worker as _vw\n"
            "_vw.worker_main = lambda argv: 0\n"
            f"sys.argv = ['sky_claw', {flag_entrada!r}, '--help']\n"
            "try:\n"
            "    import sky_claw.__main__\n"
            "except SystemExit:\n"
            "    pass"
        )
    )
    codigo = "\n".join(
        [
            "import json, subprocess, sys",
            "lanzados = []",
            "orig = subprocess.Popen",
            "class Espia(orig):",
            "    def __init__(self, *a, **kw):",
            "        lanzados.append(a[0] if a else kw.get('args'))",
            "        super().__init__(*a, **kw)",
            "subprocess.Popen = Espia",
            accion,
            "print(json.dumps({",
            "    'lanzados': lanzados,",
            "    'keyring_cargado': 'keyring' in sys.modules,",
            "    'config_cargado': 'sky_claw.config' in sys.modules,",
            "    'app_context_cargado': 'sky_claw.app_context' in sys.modules,",
            "}))",
        ]
    )
    completado = subprocess.run(  # noqa: S603 - subproceso de prueba con código fijo
        [python_directo, "-c", codigo],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": rutas_python},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    datos = json.loads(completado.stdout.strip())
    assert datos == {
        "lanzados": [],
        "keyring_cargado": False,
        "config_cargado": False,
        "app_context_cargado": False,
    }


def test_la_superficie_publica_de_sky_claw_y_app_security_se_conserva_intacta() -> None:
    """Ancla de compatibilidad: ``from sky_claw import ...`` y ``from sky_claw.app.security import ...``
    funcionan para TODOS los símbolos que ambos paquetes exportaban antes de PEP 562 ``__getattr__``."""
    import sky_claw
    import sky_claw.app.security as security_pkg

    esperados_raiz = {
        "AssetConflictDetector",
        "AssetConflictReport",
        "AssetInfo",
        "AssetType",
    }
    esperados_security = {
        "AgentGuardrail",
        "AgentGuardrailConfig",
        "AgenticLoopGuardrail",
        "GovernanceManager",
        "HITLGuard",
        "NetworkGateway",
        "PathValidator",
        "PromptArmor",
        "PromptArmorConfig",
        "PurpleScanner",
        "SecurityMetacognition",
        "TextInspector",
        "audit_resource",
        "build_system_header",
        "encapsulate_external_data",
        "run_scan",
        "safe_json_loads",
        "sandboxed_io",
        "sanitize_for_prompt",
        "scan_text",
        "secure_llm_call",
        "validate_prompt_integrity",
    }
    assert set(sky_claw.__all__) == esperados_raiz
    assert set(security_pkg.__all__) == esperados_security
    assert isinstance(sky_claw.__version__, str) and sky_claw.__version__

    # Verificar `from sky_claw import ...` y `from sky_claw.app.security import ...` en un intérprete limpio
    python_directo = getattr(sys, "_base_executable", sys.executable)
    rutas_python = os.pathsep.join([str(REPO), *(p for p in sys.path if p)])
    lista_raiz = ", ".join(sorted(esperados_raiz))
    lista_sec = ", ".join(sorted(esperados_security))
    script = "\n".join(
        [
            f"from sky_claw import __version__, {lista_raiz}",
            f"from sky_claw.app.security import {lista_sec}",
            "import sky_claw, sky_claw.app.security",
            f"assert all( getattr(sky_claw, n) is not None for n in {sorted(esperados_raiz)!r} )",
            f"assert all( getattr(sky_claw.app.security, n) is not None for n in {sorted(esperados_security)!r} )",
            "print('OK')",
        ]
    )
    res = subprocess.run(  # noqa: S603 - subproceso de prueba con código fijo
        [python_directo, "-c", script],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": rutas_python},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert res.stdout.strip() == "OK"
    with pytest.raises(AttributeError):
        _ = sky_claw.simbolo_inexistente_xyz  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        _ = security_pkg.simbolo_inexistente_xyz  # type: ignore[attr-defined]
