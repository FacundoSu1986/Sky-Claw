"""Gate adversarial del Job Object: ¿puede el worker despachar antes de estar supervisado?

Pregunta (E3-R3): el bridge crea el worker con ``organizer.startApplication`` —que
lo deja CORRIENDO— y recién DESPUÉS lo asigna al Job Object ``KILL_ON_JOB_CLOSE``.
Un proceso que ya generó descendientes antes de esa asignación los deja FUERA del
Job (Windows no adopta hijos previos): sobreviven a ``TerminateJobObject`` y al
cierre del Job, escribiendo en el output mientras el daemon cree que el árbol
terminó.

El protocolo seguro sería: *el worker no despacha ni siquiera la attestation
(que crea el proceso nieto) hasta que el daemon le confirma que el bridge ya lo
asignó al Job*. Este módulo mide, en las dos mitades, qué se cumple HOY:

* **Mitad del bridge (verde):** ``assign`` ocurre antes de ``launch_ack`` y un
  ``assign`` fallido no emite ``launch_ack``. Es la premisa que haría válido a
  ``launch_ack`` como señal de supervisión.
* **Mitad del worker/broker (hueco):** no existe ningún mensaje de supervisión.
  El ``hello_ack`` se envía al recibir el ``hello`` del worker, sin esperar al
  bridge, y el worker pasa de ahí directo a la attestation y al despacho. Con un
  bridge que NO confirmó la supervisión, el worker despacha igual.

La primera prueba del hueco es una **caracterización** (verde: el hueco existe y
queda registrado). La segunda es la **propiedad deseada** marcada
``xfail(strict=True)``: cuando se implemente el fence, ambas deben cambiarse en
el mismo PR —la caracterización pasa a rojo y el ``xfail`` estricto a XPASS—, de
modo que el fence no puede aterrizar sin tocar su propia evidencia.

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

import pytest

from sky_claw.local.mo2.plugin_bundle.skyclaw_bridge.runtime import (
    PROTOCOL_VERSION,
    BridgeCommandError,
    BridgeLaunchController,
)
from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
from sky_claw.local.mo2.vfs_broker import VfsExecutionBroker
from sky_claw.local.mo2.vfs_contracts import VFS_PROTOCOL_VERSION, VfsJob
from sky_claw.local.mo2.vfs_ipc import read_authenticated_message, write_authenticated_message
from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest
from sky_claw.local.mo2.vfs_worker import VfsToolExecution, run_worker_session

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
    def __init__(self, linea: list[str], *, fallar_asignacion: bool = False) -> None:
        self._linea = linea
        self._fallar = fallar_asignacion
        self.terminado = False
        self.cerrado = False

    def assign(self, process_handle: int) -> None:
        if self._fallar:
            raise BridgeCommandError("AssignProcessToJobObject falló: simulado")
        self._linea.append("assign")

    def terminate(self) -> None:
        self.terminado = True

    def close(self) -> None:
        self.cerrado = True


def _controlador(
    tmp_path: pathlib.Path,
    linea: list[str],
    *,
    fallar_asignacion: bool,
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
    job = _JobConLinea(linea, fallar_asignacion=fallar_asignacion)
    controlador = BridgeLaunchController(
        organizer=organizer,
        worker_executable=worker,
        worker_prefix=("--vfs-worker",),
        descriptor_path=descriptor,
        jobs_root=jobs,
        send_event=lambda evento: linea.append(str(evento.get("type") or evento.get("event"))),
        job_factory=lambda: job,
        process_waiter=lambda handle: organizer.waitForApplication(handle, False),
    )
    mensaje: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "type": "launch_worker",
        "job_id": "job-1",
        "profile": "Default",
        "manifest_path": str(manifiesto),
        "overwrite_mod": None,
    }
    return controlador, organizer, job, mensaje


def test_el_bridge_asigna_al_job_object_antes_de_emitir_launch_ack(tmp_path: pathlib.Path) -> None:
    # Arrange
    linea: list[str] = []
    controlador, organizer, _job, mensaje = _controlador(tmp_path, linea, fallar_asignacion=False)

    # Act
    controlador.launch(mensaje)
    organizer.liberar.set()
    controlador.wait_for_monitors(timeout=1)

    # Assert: el worker ya CORRÍA al asignarse (ventana inherente), y el ack va DESPUÉS del assign.
    assert linea[:3] == ["startApplication", "assign", "launch_ack"]


def test_si_la_asignacion_al_job_falla_no_hay_launch_ack_y_se_termina_al_worker(tmp_path: pathlib.Path) -> None:
    # Arrange
    linea: list[str] = []
    controlador, organizer, job, mensaje = _controlador(tmp_path, linea, fallar_asignacion=True)

    # Act
    with pytest.raises(BridgeCommandError, match="AssignProcessToJobObject"):
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


async def _observar_despacho_sin_confirmacion_de_supervision(tmp_path: pathlib.Path, *, ventana: float) -> list[str]:
    """Lanza un job contra el broker real con un bridge que RETIENE la confirmación.

    El "bridge" completa el handshake y recibe ``launch_worker``, pero jamás envía
    ``launch_ack``: es exactamente el estado de la ventana ``startApplication`` →
    ``AssignProcessToJobObject``. Devuelve, en orden, lo que el worker hizo durante
    ``ventana`` segundos: ``"probe"`` es la creación del proceso NIETO de la
    attestation y ``"handler"`` el despacho de la herramienta.
    """
    manifiesto = _manifiesto(tmp_path)
    observado: list[str] = []
    despachado = asyncio.Event()

    async def probe_espia(_ruta: pathlib.Path, sha256: str, _timeout: float) -> str:
        observado.append("probe")
        return sha256

    async def handler_espia(_manifiesto: VfsWorkerManifest) -> VfsToolExecution:
        observado.append("handler")
        despachado.set()
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
    worker: asyncio.Task[object] | None = None
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
        # El worker nace y el bridge NO confirma supervisión (no hay launch_ack).
        worker = asyncio.create_task(
            run_worker_session(
                manifest_path=pathlib.Path(str(lanzamiento["manifest_path"])),
                descriptor_path=broker.descriptor_path,
                expected_job_id=manifiesto.job.job_id,
                handlers={"health": handler_espia},
                grandchild_probe=probe_espia,
            )
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(despachado.wait(), timeout=ventana)
        return list(observado)
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


async def test_caracterizacion_hoy_el_worker_despacha_sin_que_el_bridge_confirme_la_supervision(
    tmp_path: pathlib.Path,
) -> None:
    """HUECO REGISTRADO (verde = el hueco existe): probe del nieto y herramienta corren sin ``launch_ack``.

    Si este test pasa a rojo es porque se implementó el fence: hay que invertirlo
    junto con el ``xfail`` estricto de abajo, no borrarlo.
    """
    # Arrange / Act
    observado = await _observar_despacho_sin_confirmacion_de_supervision(tmp_path, ventana=5.0)

    # Assert: el nieto (un descendiente real en producción) y la herramienta ya corrieron.
    assert observado == ["probe", "handler"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "HOLD de seguridad (E3-R3): el protocolo no tiene mensaje de supervisión; el worker crea el nieto de "
        "la attestation y despacha la herramienta sin que el bridge haya confirmado la asignación al Job "
        "Object. Fence mínimo propuesto en docs/audits/2026-11-01_e3r3_job_object_supervision_fence.md"
    ),
)
async def test_propiedad_deseada_el_worker_no_crea_descendientes_hasta_estar_supervisado(
    tmp_path: pathlib.Path,
) -> None:
    """La propiedad que el ADR 0007 da por sentada ("worker y descendientes quedan contenidos")."""
    # Arrange / Act: ventana corta; con el fence el worker queda bloqueado esperando la confirmación.
    observado = await _observar_despacho_sin_confirmacion_de_supervision(tmp_path, ventana=0.5)

    # Assert: sin confirmación de supervisión no hay probe de nieto ni despacho.
    assert observado == [], f"el worker creó descendientes/despachó sin supervisión confirmada: {observado}"
