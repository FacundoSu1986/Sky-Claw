"""Gate adversarial del Job Object: ¿puede el worker despachar antes de estar supervisado?

Pregunta (E3-R3): el bridge crea el worker con ``organizer.startApplication`` —que
lo deja CORRIENDO— y recién DESPUÉS lo asigna al Job Object ``KILL_ON_JOB_CLOSE``.
Un proceso que ya generó descendientes antes de esa asignación los deja FUERA del
Job (Windows no adopta hijos previos): sobreviven a ``TerminateJobObject`` y al
cierre del Job, escribiendo en el output mientras el daemon cree que el árbol
terminó.

El protocolo seguro: *el worker no despacha ni siquiera la attestation (que crea
el proceso nieto) hasta que el daemon le confirma que el bridge ya contuvo a todo
el árbol en el Job*. Este módulo mide, en las dos mitades, que se cumple:

* **Mitad del bridge:** el Job nace con el nombre que pidió el broker, y ``assign``
  y la adopción de los descendientes ocurren antes de ``launch_ack``. Un fallo en
  cualquiera de las dos no emite ``launch_ack`` y termina al worker. Es la premisa
  que hace válido a ``launch_ack`` como señal de supervisión.
* **Mitad del worker/broker:** el broker solo autoriza al worker tras un
  ``launch_ack`` del bridge para ESE job. Sin él, el worker sigue esperando: no
  crea el nieto de la attestation ni despacha la herramienta. Con él, lo hace, en
  ese orden.

Historia: este archivo nació en el PR #854 como medición de un HUECO. La mitad del
worker era una **caracterización** (el worker despachaba sin confirmación) más la
**propiedad deseada** marcada ``xfail(strict=True)``, y el diseño exigía que el
fence no aterrizara sin invertir su propia evidencia. Aterrizó en ``main`` (fence
#856, Job con nombre e identidad #860/#872, adopción de descendientes #877) y, al
reconciliar #854 con ``main``, se invirtió: la caracterización pasó a regresión de
la barrera y el ``xfail`` se quitó. Que estas propiedades se cumplan con dobles NO
levanta el HOLD del backend brokered: eso exige evidencia sobre el binario
congelado y el visto bueno del auditor independiente (ver el informe de E3-R3).

NO se ejecuta ningún TexGen/DynDOLOD real ni se toca un MO2 real: el worker usa un
handler espía y un probe de nieto espía, y el "bridge" es un cliente loopback.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import pathlib
import threading
from dataclasses import dataclass

import pytest

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import (
    PROTOCOL_VERSION,
    BridgeCommandError,
    BridgeLaunchController,
    JobObject,
)
from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
from sky_claw.local.mo2.vfs_broker import VfsExecutionBroker
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION, VfsJob, VfsJobResult
from sky_claw.local.mo2.vfs_ipc import read_authenticated_message, write_authenticated_message
from sky_claw.local.mo2.vfs_job_identity import PruebaDeJob
from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest
from sky_claw.local.mo2.vfs_worker import VfsToolExecution, run_worker_session

#: Formato que el bridge acepta para ``job_object`` (``Local\SkyClawVfs-<32 hex>``); el broker lo genera en producción.
_NOMBRE_DEL_JOB = "Local\\SkyClawVfs-" + "ab" * 16

# ---------------------------------------------------------------------------
# Mitad A — el bridge: assign ANTES de launch_ack
# ---------------------------------------------------------------------------


class _OrganizerConLinea:
    """``startApplication`` deja el proceso CORRIENDO y devuelve su HANDLE (API real de MO2)."""

    def __init__(self, linea: list[str]) -> None:
        self._linea = linea
        self.esperas = 0
        self.liberar = threading.Event()

    def startApplication(self, *_args: object) -> int:  # noqa: N802 - fake de API MO2
        self._linea.append("startApplication")
        return 123

    def waitForApplication(self, handle: int, refresh: bool) -> tuple[bool, int]:  # noqa: N802
        self.esperas += 1
        self.liberar.wait(timeout=1)
        return True, 0


class _JobConLinea:
    """Doble del Job: anota en ``linea`` cada paso del bridge y puede fallar en la asignación o en la adopción."""

    def __init__(self, linea: list[str], *, fallar_en: str | None = None) -> None:
        self._linea = linea
        self._fallar_en = fallar_en
        self.nombre: str | None = None
        self.terminado = False
        self.cerrado = False

    def assign(self, process_handle: int) -> None:
        if self._fallar_en == "assign":
            raise BridgeCommandError("AssignProcessToJobObject falló: simulado")
        self._linea.append("assign")

    def adopt_descendants(self, process_handle: int) -> int:
        if self._fallar_en == "adopt":
            raise BridgeCommandError("no se pudo adoptar a un descendiente: simulado")
        self._linea.append("adopt")
        return 0

    def active_processes(self) -> int:
        return 0  # el Job queda vacío al salir el worker: el monitor confirma y emite ``worker_exit`` sin terminar nada

    def terminate(self) -> None:
        self.terminado = True

    def close(self) -> None:
        self.cerrado = True


def test_el_doble_del_job_implementa_todo_el_protocolo_del_bridge() -> None:
    """Un método nuevo en ``JobObject`` rompe ESTE test, en vez de dejar un doble viejo que ya no prueba nada."""
    # Arrange / Act
    protocolo = {nombre for nombre in vars(JobObject) if not nombre.startswith("_")}

    # Assert: el protocolo congelado por igualdad literal, y el doble lo cubre entero.
    assert protocolo == {"assign", "adopt_descendants", "active_processes", "terminate", "close"}
    assert protocolo <= {nombre for nombre in dir(_JobConLinea) if not nombre.startswith("_")}


def _controlador(
    tmp_path: pathlib.Path,
    linea: list[str],
    *,
    fallar_en: str | None = None,
) -> tuple[BridgeLaunchController, _OrganizerConLinea, _JobConLinea, dict[str, object]]:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    manifiesto = jobs / "job-1.json"
    manifiesto.write_text("{}", encoding="utf-8")
    worker = tmp_path / "SkyClawApp.exe"
    worker.write_bytes(b"exe")
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text("{}", encoding="utf-8")
    organizer = _OrganizerConLinea(linea)
    job = _JobConLinea(linea, fallar_en=fallar_en)

    def crear_job(nombre: str) -> _JobConLinea:
        # El Job nace ANTES que el proceso y con el nombre que pidió el broker (el worker lo abre por ese nombre).
        linea.append("job")
        job.nombre = nombre
        return job

    controlador = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        jobs_root=jobs,
        send_event=lambda evento: linea.append(str(evento.get("type") or evento.get("event"))),
        job_factory=crear_job,
        process_waiter=lambda handle: organizer.waitForApplication(handle, False),
    )
    mensaje: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "type": "launch_worker",
        "job_id": "job-1",
        "profile": "Default",
        "manifest_path": str(manifiesto),
        "overwrite_mod": None,
        "job_object": _NOMBRE_DEL_JOB,
    }
    return controlador, organizer, job, mensaje


def test_el_bridge_asigna_y_adopta_en_el_job_object_antes_de_emitir_launch_ack(tmp_path: pathlib.Path) -> None:
    # Arrange
    linea: list[str] = []
    controlador, organizer, job, mensaje = _controlador(tmp_path, linea)

    # Act
    controlador.launch(mensaje)
    organizer.liberar.set()
    controlador.wait_for_monitors(timeout=1)

    # Assert: el Job nace primero con el nombre pedido; el worker ya CORRÍA al asignarse (ventana inherente) y el ack
    # va DESPUÉS de la asignación Y de la adopción de los descendientes que hubieran nacido antes.
    assert linea[:5] == ["job", "startApplication", "assign", "adopt", "launch_ack"]
    assert job.nombre == _NOMBRE_DEL_JOB


@pytest.mark.parametrize(
    ("fallo", "texto_del_error"),
    [
        ("assign", "AssignProcessToJobObject"),
        ("adopt", "adoptar a un descendiente"),
    ],
)
def test_si_la_contencion_en_el_job_falla_no_hay_launch_ack_y_se_termina_al_worker(
    tmp_path: pathlib.Path, fallo: str, texto_del_error: str
) -> None:
    # Arrange
    linea: list[str] = []
    controlador, organizer, job, mensaje = _controlador(tmp_path, linea, fallar_en=fallo)

    # Act
    with pytest.raises(BridgeCommandError, match=texto_del_error):
        controlador.launch(mensaje)

    # Assert: ningún ack de supervisión, y el worker sin supervisar se termina y se espera.
    assert "launch_ack" not in linea
    assert job.terminado is True
    assert job.cerrado is True
    assert organizer.esperas == 1


# ---------------------------------------------------------------------------
# Mitad B — worker + broker REAL: ¿despacha sin confirmación de supervisión?
# ---------------------------------------------------------------------------


def _manifiesto(tmp_path: pathlib.Path) -> VfsWorkerManifest:
    """Perfil MO2 mínimo con un canary visible (mismo armado que ``test_vfs_worker``)."""
    mo2 = tmp_path / "MO2"
    perfil = mo2 / "profiles" / "Default"
    mod = mo2 / "mods" / "CanaryMod"
    data_fisico = tmp_path / "Skyrim" / "Data"
    data_virtual = tmp_path / "virtual" / "Data"
    for carpeta in (perfil, mod, data_fisico, data_virtual):
        carpeta.mkdir(parents=True)
    (perfil / "modlist.txt").write_text("+CanaryMod\n", encoding="utf-8-sig")
    (mod / "canary.txt").write_bytes(b"canary")
    (data_virtual / "canary.txt").write_bytes(b"canary")
    desafio = build_attestation_challenge(mo2_root=mo2, profile="Default", physical_data_dir=data_fisico)
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id="health",
        payload={},
        timeout_seconds=10,
        expected_fingerprint=desafio.profile_fingerprint,
        mutation_targets=(),
    )
    return VfsWorkerManifest(
        protocol_version=VFS_PROTOCOL_VERSION,
        job=job,
        challenge=desafio,
        mo2_root=mo2,
        virtual_data_dir=data_virtual,
        descriptor_path=tmp_path / "descriptor.json",
    )


@dataclass(frozen=True, slots=True)
class _Observacion:
    """Qué hizo el worker REAL frente a un bridge que retiene (o da) la confirmación de supervisión."""

    #: Lo que hizo durante la ventana SIN ``launch_ack``: ``"probe"`` = el proceso NIETO de la attestation,
    #: ``"handler"`` = el despacho de la herramienta.
    sin_ack: tuple[str, ...]
    #: ¿El worker seguía vivo esperando al terminar la ventana? Sin esto, "no despachó" no distingue una barrera
    #: que retiene de un worker que cayó por otra causa (lo que ocultaba el ``xfail`` viejo).
    esperando: bool
    #: Lo que había hecho tras el ``launch_ack`` (``None`` si no se confirmó).
    con_ack: tuple[str, ...] | None
    #: ``True`` si, tras el ``launch_ack``, la sesión del worker terminó con éxito (``None`` si no se confirmó).
    exito: bool | None


async def _observar_al_worker_ante_la_confirmacion_de_supervision(
    tmp_path: pathlib.Path, *, ventana: float, confirmar: bool
) -> _Observacion:
    """Lanza un job contra el broker real con un bridge que RETIENE la confirmación durante ``ventana`` segundos.

    El "bridge" completa el handshake y recibe ``launch_worker``, pero no envía
    ``launch_ack``: es exactamente el estado de la ventana ``startApplication`` →
    ``AssignProcessToJobObject`` → adopción. Con ``confirmar=True`` envía después el
    ``launch_ack`` (control positivo: la barrera es ese mensaje y no otra cosa).
    """
    manifiesto = _manifiesto(tmp_path)
    observado: list[str] = []

    async def probe_espia(_ruta: pathlib.Path, sha256: str, _timeout: float) -> str:
        observado.append("probe")
        return sha256

    async def handler_espia(_manifiesto: VfsWorkerManifest) -> VfsToolExecution:
        observado.append("handler")
        return VfsToolExecution.ok()

    broker = VfsExecutionBroker(
        instance_id="portable-main",
        state_dir=tmp_path / "state",
        secret=b"x" * 32,
        descriptor_hardener=lambda _ruta: None,
    )
    await broker.start()
    descriptor = json.loads(broker.descriptor_path.read_text(encoding="utf-8"))
    secreto = base64.urlsafe_b64decode(descriptor["token"])
    lector_bridge, escritor_bridge = await asyncio.open_connection(descriptor["host"], descriptor["port"])
    pendiente: asyncio.Task[object] | None = None
    worker: asyncio.Task[VfsJobResult | None] | None = None
    try:
        await write_authenticated_message(
            escritor_bridge,
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "hello",
                "role": "bridge",
                "instance_id": "portable-main",
                "session_id": descriptor["session_id"],
            },
            secreto,
        )
        await read_authenticated_message(lector_bridge, secreto)
        await broker.wait_until_ready(timeout=1)
        pendiente = asyncio.create_task(
            broker.submit(
                manifiesto.job,
                challenge=manifiesto.challenge,
                mo2_root=manifiesto.mo2_root,
                virtual_data_dir=manifiesto.virtual_data_dir,
            )
        )
        lanzamiento = await read_authenticated_message(lector_bridge, secreto)
        assert lanzamiento["type"] == "launch_worker"
        # El worker nace ya verificado en el Job que pidió el broker, y el bridge NO confirma supervisión (no hay
        # launch_ack): es la ventana en la que el worker YA corre pero el árbol aún no está contenido.
        nombre_del_job = str(lanzamiento["job_object"])
        worker = asyncio.create_task(
            run_worker_session(
                manifest_path=pathlib.Path(str(lanzamiento["manifest_path"])),
                descriptor_path=broker.descriptor_path,
                expected_job_id=manifiesto.job.job_id,
                job_identity=PruebaDeJob(nombre=nombre_del_job, pid=1234, procesos_totales=2, procesos_activos=2),
                handlers={"health": handler_espia},
                grandchild_probe=probe_espia,
            )
        )
        await asyncio.sleep(ventana)
        sin_ack = tuple(observado)
        esperando = not worker.done()
        if not confirmar:
            return _Observacion(sin_ack=sin_ack, esperando=esperando, con_ack=None, exito=None)
        await write_authenticated_message(
            escritor_bridge,
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "launch_ack",
                "job_id": manifiesto.job.job_id,
                "job_object": nombre_del_job,
            },
            secreto,
        )
        resultado = await asyncio.wait_for(worker, timeout=6.0)
        return _Observacion(
            sin_ack=sin_ack,
            esperando=esperando,
            con_ack=tuple(observado),
            exito=resultado is not None and resultado.success is True,
        )
    finally:
        # Cierre determinista de lo que haya quedado vivo (despachó o quedó esperando).
        if worker is not None:
            if not worker.done():
                with contextlib.suppress(TimeoutError, Exception):
                    await asyncio.wait_for(asyncio.shield(worker), timeout=2.0)
            if not worker.done():
                worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker
        with contextlib.suppress(Exception):
            await write_authenticated_message(
                escritor_bridge,
                {
                    "protocol_version": VFS_PROTOCOL_VERSION,
                    "type": "event",
                    "event": "worker_exit",
                    "job_id": manifiesto.job.job_id,
                    "wait_ok": True,
                    "exit_code": 0,
                },
                secreto,
            )
        if pendiente is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(pendiente, timeout=5.0)
        escritor_bridge.close()
        with contextlib.suppress(Exception):
            await escritor_bridge.wait_closed()
        await broker.close()


async def test_regresion_de_la_barrera_el_worker_no_despacha_sin_que_el_bridge_confirme_la_supervision(
    tmp_path: pathlib.Path,
) -> None:
    """BARRERA (regresión del hueco que este archivo caracterizaba en #854): sin ``launch_ack`` no hay nieto ni despacho.

    Antes de la barrera este test afirmaba lo contrario (``["probe", "handler"]``). Si vuelve a ponerse rojo es que
    alguien quitó la barrera del broker/worker: se arregla el código, no el test.
    """
    # Arrange / Act: el bridge retiene la confirmación durante toda la ventana.
    observacion = await _observar_al_worker_ante_la_confirmacion_de_supervision(tmp_path, ventana=1.0, confirmar=False)

    # Assert: ni el nieto (un descendiente real en producción) ni la herramienta corrieron, y el worker sigue
    # esperando (no cayó por otra causa: sin esto la ausencia de despacho no probaría la barrera).
    assert observacion.sin_ack == (), f"el worker creó descendientes/despachó sin supervisión: {observacion.sin_ack}"
    assert observacion.esperando, "el worker terminó solo durante la ventana: la barrera no se está ejerciendo"


async def test_propiedad_el_worker_no_crea_descendientes_hasta_estar_supervisado(tmp_path: pathlib.Path) -> None:
    """La propiedad que el ADR 0007 da por sentada ("worker y descendientes quedan contenidos"), con control positivo."""
    # Arrange / Act: ventana sin confirmación y, a continuación, el ``launch_ack`` del bridge.
    observacion = await _observar_al_worker_ante_la_confirmacion_de_supervision(tmp_path, ventana=0.5, confirmar=True)

    # Assert: nada antes del ack; el nieto y la herramienta, en ese orden, DESPUÉS (la barrera es ese mensaje).
    assert observacion.sin_ack == ()
    assert observacion.esperando
    assert observacion.con_ack == ("probe", "handler")
    assert observacion.exito is True
