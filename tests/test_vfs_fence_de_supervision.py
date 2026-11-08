"""Fence de supervisión del worker: sin confirmación del bridge no hay nietos ni herramientas.

El bridge hace ``startApplication`` (el worker nace CORRIENDO) y *después* asigna el
proceso al Job Object ``KILL_ON_JOB_CLOSE``; recién entonces emite ``launch_ack``. Un
descendiente creado antes de esa asignación queda fuera del Job y sobrevive a su
terminación. Antes de este fence nada ordenaba el despacho del worker después del
``launch_ack``: el broker contestaba ``hello_ack`` sin esperar al bridge y el worker
pasaba directo a la attestation (que crea un proceso NIETO) y a la herramienta.

Propiedad del mecanismo (no un recordatorio): **el worker no crea el nieto de la attestation
ni despacha la herramienta hasta recibir un mensaje ``supervised`` autenticado y ligado a su ``job_id``, y el
broker solo lo envía tras un ``launch_ack`` del bridge para ESE job, mientras el job
siga vivo y no esté siendo cancelado.**

Los tests ejercen el broker REAL y el ``run_worker_session`` REAL; el bridge se simula
con un cliente de socket que habla el protocolo autenticado y decide cuándo (o si)
confirma. Las pruebas de contención del sistema operativo viven en
``test_vfs_job_containment_windows.py``: estas no las sustituyen.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import json
import pathlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from sky_claw.local.mo2.vfs_attestation import build_attestation_challenge
from sky_claw.local.mo2.vfs_broker import VfsBridgeLaunchError, VfsExecutionBroker, VfsWorkerDisconnectedError
from sky_claw.local.mo2.vfs_contracts import (
    ALLOWED_VFS_SESSION_TOOL_IDS,
    ALLOWED_VFS_TOOL_IDS,
    VFS_PROTOCOL_VERSION,
    VfsJob,
)
from sky_claw.local.mo2.vfs_ipc import VfsFrameError, read_authenticated_message, write_authenticated_message
from sky_claw.local.mo2.vfs_job_identity import PruebaDeJob
from sky_claw.local.mo2.vfs_manifest import VfsWorkerManifest
from sky_claw.local.mo2.vfs_worker import (
    VfsToolExecution,
    VfsWorkerBootstrapError,
    _esperar_supervision,
    run_worker_session,
)

_VENTANA_DE_ESPERA = 0.4  # un worker que despacha lo hace en milisegundos: 0.4 s sobra para detectarlo

#: «Usar la identidad que el broker eligió para este job»: distinto de ``None`` (= omitir el campo a propósito).
_DEL_JOB = object()


def _prueba_de_job(nombre: str) -> PruebaDeJob:
    """Lo que devolvería ``verificar_identidad_del_job`` para un worker REAL ya verificado (doble de la prueba)."""
    return PruebaDeJob(nombre=nombre, pid=1234, procesos_totales=2, procesos_activos=2)


def _manifiesto(tmp_path: pathlib.Path, *, tool_id: str = "health", timeout_seconds: int = 15) -> VfsWorkerManifest:
    """Perfil MO2 mínimo con un canary visible bajo el Data virtual (como ``test_vfs_worker``)."""
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
    payload: dict[str, object] = {}
    if tool_id in ALLOWED_VFS_SESSION_TOOL_IDS:
        nombre = {"texgen": "TexGenx64.exe", "dyndolod": "DynDOLODx64.exe"}[tool_id]
        payload = {"executable": str(tmp_path / nombre), "argv": ["-sse"], "cwd": str(tmp_path)}
    job = VfsJob.create(
        instance_id="portable-main",
        profile="Default",
        tool_id=tool_id,
        payload=payload,
        timeout_seconds=timeout_seconds,
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


async def _hasta(condicion: Callable[[], bool], *, timeout: float = 3.0) -> None:
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if condicion():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("la condición no se cumplió a tiempo")


class _WorkerCrudo:
    """Un 'worker' que habla el protocolo a mano: sirve para ver QUÉ recibe del broker y CUÁNDO."""

    def __init__(self, lector: asyncio.StreamReader, escritor: asyncio.StreamWriter, secreto: bytes) -> None:
        self._lector = lector
        self._escritor = escritor
        self._secreto = secreto
        self.recibidos: list[dict[str, Any]] = []

    async def leer(self, *, timeout: float = _VENTANA_DE_ESPERA) -> dict[str, Any] | None:
        try:
            mensaje = await asyncio.wait_for(read_authenticated_message(self._lector, self._secreto), timeout)
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, VfsFrameError):
            return None  # nada recibido: plazo vencido o el broker cerró la conexión
        self.recibidos.append(dict(mensaje))
        return dict(mensaje)

    async def cerrar(self) -> None:
        self._escritor.close()
        with contextlib.suppress(Exception):
            await self._escritor.wait_closed()


class _Arnes:
    """Broker real + bridge simulado por socket autenticado. Decide cuándo se confirma la supervisión."""

    def __init__(self, tmp_path: pathlib.Path, *, tool_id: str = "health") -> None:
        self.tmp_path = tmp_path
        self.tool_id = tool_id
        self.tarea: asyncio.Task[Any] | None = None
        self._auxiliares: list[asyncio.Task[Any]] = []
        self._conexiones: list[asyncio.StreamWriter] = []

    async def __aenter__(self) -> _Arnes:
        self.manifiesto = _manifiesto(self.tmp_path, tool_id=self.tool_id)
        self.broker = VfsExecutionBroker(
            instance_id="portable-main",
            state_dir=self.tmp_path / "state",
            secret=b"x" * 32,
            descriptor_hardener=lambda _ruta: None,
            # Teardown acotado: los caminos de fallo no deben esperar los 30 s de producción.
            fence_grace_seconds=0.5,
            connected_worker_exit_deadline_seconds=1.5,
            bridge_reconnect_replay_grace_seconds=0.2,
        )
        await self.broker.start()
        self.descriptor = json.loads(self.broker.descriptor_path.read_text(encoding="utf-8"))
        self.secreto = base64.urlsafe_b64decode(self.descriptor["token"])
        await self._conectar_bridge()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        with contextlib.suppress(Exception):
            await self.worker_exit()  # desbloquea el fence terminal del broker en los caminos de fallo
        for tarea in [self.tarea, *self._auxiliares]:
            if tarea is not None and not tarea.done():
                tarea.cancel()
        for tarea in [self.tarea, *self._auxiliares]:
            if tarea is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(tarea, timeout=4)
        for escritor in self._conexiones:
            escritor.close()
        for escritor in self._conexiones:
            with contextlib.suppress(Exception):
                await escritor.wait_closed()
        with contextlib.suppress(Exception):
            await self.broker.close()
        await asyncio.sleep(0.05)  # deja terminar los handlers de conexión del broker

    @property
    def job_id(self) -> str:
        return self.manifiesto.job.job_id

    async def _conectar_bridge(self) -> None:
        self.lector_bridge, self.escritor_bridge = await asyncio.open_connection(
            self.descriptor["host"], self.descriptor["port"]
        )
        self._conexiones.append(self.escritor_bridge)
        await write_authenticated_message(
            self.escritor_bridge,
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "hello",
                "role": "bridge",
                "instance_id": "portable-main",
                "session_id": self.descriptor["session_id"],
            },
            self.secreto,
        )
        await read_authenticated_message(self.lector_bridge, self.secreto)
        await self.broker.wait_until_ready(timeout=2)

    async def caer_el_bridge(self) -> None:
        self.escritor_bridge.close()
        with contextlib.suppress(Exception):
            await self.escritor_bridge.wait_closed()
        await _hasta(lambda: self.broker._bridge_writer is None)

    async def reconectar_bridge(self) -> None:
        await self._conectar_bridge()

    async def lanzar_job(self) -> dict[str, Any]:
        """Somete el job y devuelve el ``launch_worker`` que recibe el bridge (el worker aún no existe)."""
        job = self.manifiesto.job
        argumentos: dict[str, Any] = {
            "challenge": self.manifiesto.challenge,
            "mo2_root": self.manifiesto.mo2_root,
            "virtual_data_dir": self.manifiesto.virtual_data_dir,
        }
        if self.tool_id in ALLOWED_VFS_SESSION_TOOL_IDS:
            coro: Awaitable[Any] = self.broker.open_session(job, **argumentos)
        else:
            coro = self.broker.submit(job, **argumentos)
        self.tarea = asyncio.create_task(coro)
        lanzamiento = await read_authenticated_message(self.lector_bridge, self.secreto)
        assert lanzamiento["type"] == "launch_worker"
        self.job_object: str = lanzamiento["job_object"]
        return dict(lanzamiento)

    async def enviar_bridge(self, mensaje: dict[str, Any]) -> None:
        await write_authenticated_message(self.escritor_bridge, mensaje, self.secreto)

    async def launch_ack(self, job_id: str | None = None, *, veces: int = 1, job_object: object = _DEL_JOB) -> None:
        """``job_object``: ``_DEL_JOB`` (el pedido), un nombre ajeno, o ``None`` para omitir el campo (bridge viejo)."""
        for _ in range(veces):
            mensaje: dict[str, Any] = {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "launch_ack",
                "job_id": job_id or self.job_id,
            }
            identidad = self.job_object if job_object is _DEL_JOB else job_object
            if identidad is not None:
                mensaje["job_object"] = identidad
            await self.enviar_bridge(mensaje)

    async def error_de_asignacion(self) -> None:
        """Lo que el bridge real emite si ``AssignProcessToJobObject`` falla (ver ``bridge_error_event``)."""
        await self.enviar_bridge(
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "event",
                "event": "bridge_error",
                "command": "launch_worker",
                "job_id": self.job_id,
                "message": "AssignProcessToJobObject falló: simulado",
                "kind": "rejected",
            }
        )

    async def worker_exit(self, *, exit_code: int = 0) -> None:
        await self.enviar_bridge(
            {
                "protocol_version": VFS_PROTOCOL_VERSION,
                "type": "event",
                "event": "worker_exit",
                "job_id": self.job_id,
                "wait_ok": True,
                "exit_code": exit_code,
            }
        )

    async def siguiente_del_bridge(self, *, timeout: float = 5.0) -> dict[str, Any]:
        return dict(await asyncio.wait_for(read_authenticated_message(self.lector_bridge, self.secreto), timeout))

    def worker_real(self, observado: list[str], *, supervision_timeout: float = 30.0) -> asyncio.Task[Any]:
        """``run_worker_session`` REAL con espías: ``probe`` = el proceso NIETO, ``handler`` = la herramienta."""

        async def probe(_ruta: pathlib.Path, sha256: str, _timeout: float) -> str:
            observado.append("probe")
            return sha256

        async def handler(_manifiesto: VfsWorkerManifest) -> VfsToolExecution:
            observado.append("handler")
            return VfsToolExecution.ok()

        tarea = asyncio.create_task(
            run_worker_session(
                manifest_path=self.broker._jobs_dir / f"{self.job_id}.json",
                descriptor_path=self.broker.descriptor_path,
                expected_job_id=self.job_id,
                handlers={"health": handler},
                grandchild_probe=probe,
                supervision_timeout=supervision_timeout,
                job_identity=_prueba_de_job(self.job_object),
            )
        )
        self._auxiliares.append(tarea)
        return tarea

    async def worker_crudo(self, *, con_capacidad: bool = True, job_object: object = _DEL_JOB) -> _WorkerCrudo:
        """``job_object``: ``_DEL_JOB`` (el pedido), un nombre ajeno, o ``None`` para omitir el campo."""
        lector, escritor = await asyncio.open_connection(self.descriptor["host"], self.descriptor["port"])
        self._conexiones.append(escritor)
        hola: dict[str, Any] = {
            "protocol_version": VFS_PROTOCOL_VERSION,
            "type": "hello",
            "role": "worker",
            "instance_id": "portable-main",
            "session_id": self.descriptor["session_id"],
            "job_id": self.job_id,
        }
        if con_capacidad:
            hola["supervision_fence"] = True
        identidad = self.job_object if job_object is _DEL_JOB else job_object
        if identidad is not None:
            hola["job_object"] = identidad
        await write_authenticated_message(escritor, hola, self.secreto)
        return _WorkerCrudo(lector, escritor, self.secreto)

    async def esperar_worker_conectado(self, worker: asyncio.Task[Any] | None = None) -> None:
        """Espera a que el worker esté conectado — o a que ya haya terminado (en cuyo caso el test lo nota)."""
        await _hasta(lambda: self.job_id in self.broker._worker_by_job or (worker is not None and worker.done()))


# ---------------------------------------------------------------------------
# 2. Worker sin confirmación: cero nietos y cero herramientas
# ---------------------------------------------------------------------------


async def test_el_worker_no_crea_nietos_ni_despacha_hasta_recibir_la_confirmacion(tmp_path: pathlib.Path) -> None:
    # Arrange
    observado: list[str] = []
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        worker = arnes.worker_real(observado)
        await arnes.esperar_worker_conectado(worker)

        # Act 1: ventana sin confirmación del bridge.
        await asyncio.sleep(_VENTANA_DE_ESPERA)
        antes_del_ack = list(observado)
        # Act 2: el bridge confirma.
        await arnes.launch_ack()
        resultado_del_worker = await asyncio.wait_for(worker, timeout=6)
        await arnes.worker_exit()
        resultado = await asyncio.wait_for(arnes.tarea, timeout=6)

        # Assert: nada antes del ack; probe (nieto) y herramienta, en ese orden, después.
        assert antes_del_ack == [], f"el worker creó descendientes/despachó sin supervisión confirmada: {antes_del_ack}"
        assert observado == ["probe", "handler"]
        assert resultado_del_worker is not None and resultado_del_worker.success is True
        assert resultado.success is True


# ---------------------------------------------------------------------------
# 1. launch_ack antes o después del hello del worker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("orden", ["ack_antes_del_hello", "ack_despues_del_hello"])
async def test_el_ack_autoriza_al_worker_llegue_antes_o_despues_de_su_hello(tmp_path: pathlib.Path, orden: str) -> None:
    # Arrange
    observado: list[str] = []
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()

        # Act
        if orden == "ack_antes_del_hello":
            await arnes.launch_ack()
            await _hasta(lambda: arnes.job_id in arnes.broker._launch_acked)
            worker = arnes.worker_real(observado)
        else:
            worker = arnes.worker_real(observado)
            await arnes.esperar_worker_conectado(worker)
            await arnes.launch_ack()
        await asyncio.wait_for(worker, timeout=6)
        await arnes.worker_exit()
        resultado = await asyncio.wait_for(arnes.tarea, timeout=6)

        # Assert
        assert observado == ["probe", "handler"]
        assert resultado.success is True


# ---------------------------------------------------------------------------
# 3. launch_ack de otro job: rechazo
# ---------------------------------------------------------------------------


async def test_un_launch_ack_de_otro_job_no_autoriza_a_este_worker(tmp_path: pathlib.Path) -> None:
    # Arrange
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        crudo = await arnes.worker_crudo()
        assert (await crudo.leer(timeout=5))["type"] == "hello_ack"  # type: ignore[index]

        # Act: el bridge confirma OTRO job (que además no existe en el broker).
        await arnes.launch_ack("job-ajeno")
        recibido_con_ack_ajeno = await crudo.leer()
        await arnes.launch_ack()  # el correcto
        recibido_con_ack_propio = await crudo.leer(timeout=5)

        # Assert
        assert recibido_con_ack_ajeno is None, "un ack de otro job no puede autorizar a este worker"
        assert recibido_con_ack_propio is not None
        assert recibido_con_ack_propio["type"] == "supervised"
        assert recibido_con_ack_propio["job_id"] == arnes.job_id
        assert "job-ajeno" not in arnes.broker._launch_acked, "no se acumulan acks de jobs desconocidos"


async def test_el_worker_rechaza_un_supervised_atribuido_a_otro_job(tmp_path: pathlib.Path) -> None:
    # Arrange: el broker (suplantado) manda `supervised` con el job_id equivocado.
    secreto = b"s" * 32
    servidor_listo: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def servidor(_lector: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
        await write_authenticated_message(
            escritor,
            {"protocol_version": VFS_PROTOCOL_VERSION, "type": "supervised", "job_id": "job-ajeno"},
            secreto,
        )
        await escritor.drain()
        servidor_listo.set_result(None)
        escritor.close()  # el lado servidor también se cierra: sin transportes huérfanos
        with contextlib.suppress(Exception):
            await escritor.wait_closed()

    srv = await asyncio.start_server(servidor, "127.0.0.1", 0)
    puerto = srv.sockets[0].getsockname()[1]
    lector, escritor = await asyncio.open_connection("127.0.0.1", puerto)
    try:
        # Act / Assert
        with pytest.raises(VfsWorkerBootstrapError, match="job"):
            await _esperar_supervision(lector, secret=secreto, job_id="job-propio", timeout=2.0)
    finally:
        escritor.close()
        with contextlib.suppress(Exception):
            await escritor.wait_closed()
        srv.close()
        await srv.wait_closed()


# ---------------------------------------------------------------------------
# 4. Confirmación duplicada: idempotente
# ---------------------------------------------------------------------------


async def test_una_confirmacion_duplicada_entrega_un_solo_supervised(tmp_path: pathlib.Path) -> None:
    # Arrange
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        crudo = await arnes.worker_crudo()
        await crudo.leer(timeout=5)  # hello_ack

        # Act: el bridge reenvía el ack (reconexión con reenvío de eventos pendientes).
        await arnes.launch_ack(veces=3)
        primero = await crudo.leer(timeout=5)
        extra = await crudo.leer()

        # Assert: el worker recibe UN solo `supervised`.
        assert primero is not None and primero["type"] == "supervised"
        assert extra is None, f"llegó un segundo mensaje por el ack duplicado: {extra}"


async def test_una_confirmacion_duplicada_no_despacha_dos_veces(tmp_path: pathlib.Path) -> None:
    # Arrange
    observado: list[str] = []
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        worker = arnes.worker_real(observado)
        await arnes.esperar_worker_conectado(worker)

        # Act
        await arnes.launch_ack(veces=3)
        await asyncio.wait_for(worker, timeout=6)
        await arnes.worker_exit()
        resultado = await asyncio.wait_for(arnes.tarea, timeout=6)  # type: ignore[arg-type]

        # Assert
        assert observado == ["probe", "handler"]
        assert resultado.success is True


# ---------------------------------------------------------------------------
# 5. Fallo de AssignProcessToJobObject: no autorizar la ejecución
# ---------------------------------------------------------------------------


async def test_si_falla_la_asignacion_al_job_object_no_se_autoriza_la_ejecucion(tmp_path: pathlib.Path) -> None:
    # Arrange
    observado: list[str] = []
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        # Plazo largo a propósito: solo la DENEGACIÓN explícita del broker puede terminar al worker a tiempo.
        worker = arnes.worker_real(observado, supervision_timeout=30.0)
        await arnes.esperar_worker_conectado(worker)

        # Act: el bridge informa el fallo del assign y, inconsistentemente, un ack posterior.
        await arnes.error_de_asignacion()
        with pytest.raises(VfsBridgeLaunchError, match="AssignProcessToJobObject"):
            await asyncio.wait_for(arnes.tarea, timeout=5)
        await arnes.launch_ack()
        with pytest.raises(VfsWorkerBootstrapError):
            await asyncio.wait_for(worker, timeout=5)

        # Assert
        assert observado == [], "una asignación fallida jamás habilita al worker"


# ---------------------------------------------------------------------------
# 6. Timeout de supervisión: fallo cerrado
# ---------------------------------------------------------------------------


async def test_el_timeout_de_supervision_falla_cerrado_sin_despachar(tmp_path: pathlib.Path) -> None:
    # Arrange
    observado: list[str] = []
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        worker = arnes.worker_real(observado, supervision_timeout=0.3)

        # Act / Assert 1: sin ack el worker NO prosigue al vencer el plazo: aborta.
        with pytest.raises(VfsWorkerBootstrapError, match="supervisi"):
            await asyncio.wait_for(worker, timeout=5)
        assert observado == []

        # Assert 2 (daemon): el bridge informa que el worker terminó (código 70) sin resultado y el job FALLA;
        # jamás se reporta éxito de un worker que no llegó a estar supervisado.
        await arnes.worker_exit(exit_code=70)
        with pytest.raises(VfsWorkerDisconnectedError):
            await asyncio.wait_for(arnes.tarea, timeout=6)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 7. Desconexión o caída del bridge antes de la confirmación
# ---------------------------------------------------------------------------


async def test_la_caida_del_bridge_antes_de_la_confirmacion_no_autoriza(tmp_path: pathlib.Path) -> None:
    # Arrange
    observado: list[str] = []
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        worker = arnes.worker_real(observado, supervision_timeout=0.8)
        await arnes.esperar_worker_conectado(worker)

        # Act: el bridge cae sin haber confirmado.
        await arnes.caer_el_bridge()

        # Assert
        with pytest.raises(VfsWorkerBootstrapError):
            await asyncio.wait_for(worker, timeout=5)
        assert observado == []


async def test_un_ack_tras_la_reconexion_del_bridge_autoriza_una_sola_vez(tmp_path: pathlib.Path) -> None:
    """El bridge real reencola los eventos no enviados: un ack legítimo puede llegar tras reconectar."""
    # Arrange
    observado: list[str] = []
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        worker = arnes.worker_real(observado)
        await arnes.esperar_worker_conectado(worker)
        await arnes.caer_el_bridge()
        await asyncio.sleep(_VENTANA_DE_ESPERA)
        antes = list(observado)

        # Act
        await arnes.reconectar_bridge()
        await arnes.launch_ack(veces=2)
        await asyncio.wait_for(worker, timeout=6)
        await arnes.worker_exit()
        resultado = await asyncio.wait_for(arnes.tarea, timeout=6)

        # Assert
        assert antes == []
        assert observado == ["probe", "handler"]
        assert resultado.success is True


# ---------------------------------------------------------------------------
# 8. Cancelación concurrente con confirmación tardía
# ---------------------------------------------------------------------------


async def test_una_confirmacion_tardia_durante_la_cancelacion_no_autoriza_al_worker(tmp_path: pathlib.Path) -> None:
    # Arrange: el worker espera la supervisión cuando se cancela el job.
    observado: list[str] = []
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        worker = arnes.worker_real(observado)
        await arnes.esperar_worker_conectado(worker)

        # Act: cancelación; mientras el broker espera el worker_exit llega el ack TARDÍO.
        arnes.tarea.cancel()  # type: ignore[union-attr]
        cancelacion = await arnes.siguiente_del_bridge()
        await arnes.launch_ack()
        resultado_del_worker = await asyncio.wait_for(worker, timeout=5)
        await arnes.worker_exit()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(arnes.tarea, timeout=6)  # type: ignore[arg-type]

        # Assert
        assert cancelacion["type"] == "cancel"
        assert resultado_del_worker is None, "el worker salió por cancelación"
        assert observado == [], "una confirmación tardía no revive a un worker cancelado"


async def test_un_worker_que_conecta_tarde_tras_la_cancelacion_no_es_autorizado(tmp_path: pathlib.Path) -> None:
    """El ack ya estaba registrado cuando se canceló: el hello tardío del worker no puede revivirlo."""
    # Arrange: el bridge confirmó, y recién DESPUÉS se cancela el job (todavía sin worker conectado).
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        await arnes.launch_ack()
        await _hasta(lambda: arnes.job_id in arnes.broker._launch_acked)
        arnes.tarea.cancel()  # type: ignore[union-attr]
        cancelacion = await arnes.siguiente_del_bridge()
        assert cancelacion["type"] == "cancel"

        # Act: el worker llega tarde (su arranque fue más lento que la cancelación).
        tardio = await arnes.worker_crudo()
        hello_ack = await tardio.leer(timeout=5)
        recibido = await tardio.leer()
        await arnes.worker_exit()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(arnes.tarea, timeout=6)  # type: ignore[arg-type]

        # Assert
        assert hello_ack is not None and hello_ack["type"] == "hello_ack"
        assert recibido is None, f"un job en cancelación autorizó a un worker tardío: {recibido}"


async def test_una_cancelacion_registrada_mientras_se_espera_el_lock_no_deja_pasar_supervised(
    tmp_path: pathlib.Path,
) -> None:
    """Hallazgo de revisión (P2): chequear la cancelación ANTES de esperar ``_write_lock`` no alcanza.

    Con el lock ocupado, la autorización pasa sus chequeos y se suspende esperándolo. Si en esa
    suspensión se registra la cancelación, su ``cancel`` queda DETRÁS de ``supervised`` en la cola
    (``asyncio.Lock`` es FIFO) y el worker sería autorizado con el job ya cancelándose. El estado se
    rechequea dentro del lock, justo antes de escribir.
    """
    # Arrange: worker conectado y el lock de escritura del broker tomado por otro emisor.
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        crudo = await arnes.worker_crudo()
        assert (await crudo.leer(timeout=5))["type"] == "hello_ack"  # type: ignore[index]
        await arnes.broker._write_lock.acquire()
        try:
            # Act 1: llega el ack; la autorización pasa los chequeos y queda esperando el lock.
            await arnes.launch_ack()
            await _hasta(lambda: arnes.job_id in arnes.broker._supervised_sent)
            # Act 2: en esa suspensión se cancela el job (misma ruta de producción que los demás tests).
            arnes.tarea.cancel()  # type: ignore[union-attr]
            await _hasta(lambda: arnes.job_id in arnes.broker._termination_tasks)
        finally:
            arnes.broker._write_lock.release()

        # Assert: lo primero (y único) que recibe el worker es el cancel; jamás supervised.
        primero = await crudo.leer(timeout=5)
        siguiente = await crudo.leer()
        await arnes.worker_exit()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(arnes.tarea, timeout=6)  # type: ignore[arg-type]
        assert primero is not None and primero["type"] == "cancel", f"se esperaba cancel, llegó: {primero}"
        assert "supervised" not in [m["type"] for m in crudo.recibidos], "un job en cancelación fue autorizado"
        assert siguiente is None


async def test_un_job_ya_resuelto_no_autoriza_a_un_worker_que_llega_tarde(tmp_path: pathlib.Path) -> None:
    """El ack ya estaba registrado, pero el job terminó (futuro resuelto) antes del hello del worker."""
    # Arrange: el bridge confirmó y el job falla (futuro resuelto) mientras el broker espera el worker_exit.
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        await arnes.launch_ack()
        await _hasta(lambda: arnes.job_id in arnes.broker._launch_acked)
        arnes.broker._pending[arnes.job_id].set_exception(VfsBridgeLaunchError("job resuelto antes del worker"))
        await _hasta(lambda: arnes.broker._pending[arnes.job_id].done())

        # Act: el worker llega después de que el job quedó resuelto.
        tardio = await arnes.worker_crudo()
        hello_ack = await tardio.leer(timeout=5)
        recibido = await tardio.leer()
        await arnes.worker_exit()
        with pytest.raises(VfsBridgeLaunchError):
            await asyncio.wait_for(arnes.tarea, timeout=6)  # type: ignore[arg-type]

        # Assert
        assert hello_ack is not None and hello_ack["type"] == "hello_ack"
        assert recibido is None, f"un job ya resuelto autorizó a un worker tardío: {recibido}"


# ---------------------------------------------------------------------------
# 10. Compatibilidad: toda herramienta del broker (LOOT, vfs-health, TexGen, DynDOLOD)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_id", sorted(ALLOWED_VFS_TOOL_IDS))
async def test_toda_herramienta_del_broker_espera_la_misma_confirmacion(tmp_path: pathlib.Path, tool_id: str) -> None:
    """Se enumera la familia COMPLETA de tool_ids: un cuarto consumidor del broker nace con el fence."""
    # Arrange: submit (health, loot_sort) y open_session (texgen, dyndolod) comparten el mismo fence.
    async with _Arnes(tmp_path, tool_id=tool_id) as arnes:
        await arnes.lanzar_job()
        crudo = await arnes.worker_crudo()
        assert (await crudo.leer(timeout=5))["type"] == "hello_ack"  # type: ignore[index]

        # Act
        sin_ack = await crudo.leer()
        await arnes.launch_ack()
        con_ack = await crudo.leer(timeout=5)

        # Assert
        assert sin_ack is None
        assert con_ack is not None and con_ack["type"] == "supervised" and con_ack["job_id"] == arnes.job_id


def test_la_familia_de_herramientas_no_cambio_sin_revisar_el_fence() -> None:
    # Arrange / Act / Assert: igualdad literal (enumerar, no muestrear).
    assert frozenset({"health", "loot_sort", "texgen", "dyndolod"}) == ALLOWED_VFS_TOOL_IDS


# ---------------------------------------------------------------------------
# Un worker sin la capacidad no puede correr bajo este broker (sin fallback silencioso)
# ---------------------------------------------------------------------------


async def test_un_worker_sin_la_capacidad_de_fence_es_rechazado_por_el_broker(tmp_path: pathlib.Path) -> None:
    """Un worker viejo despacharía sin esperar: el broker no puede ejecutar un job con él."""
    # Arrange
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        viejo = await arnes.worker_crudo(con_capacidad=False)

        # Act
        respuesta = await viejo.leer(timeout=2)
        await arnes.launch_ack()
        despues_del_ack = await viejo.leer()

        # Assert: ni siquiera hay hello_ack, y el ack no le entrega nada.
        assert respuesta is None
        assert despues_del_ack is None
        assert arnes.job_id not in arnes.broker._worker_by_job


# ---------------------------------------------------------------------------
# Higiene del estado del broker
# ---------------------------------------------------------------------------


async def test_el_estado_de_supervision_se_limpia_al_terminar_el_job(tmp_path: pathlib.Path) -> None:
    # Arrange
    observado: list[str] = []
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        worker = arnes.worker_real(observado)
        await arnes.esperar_worker_conectado(worker)
        await arnes.launch_ack()
        await _hasta(lambda: arnes.job_id in arnes.broker._launch_acked)  # el estado SÍ se registró...
        await asyncio.wait_for(worker, timeout=6)
        await arnes.worker_exit()

        # Act
        await asyncio.wait_for(arnes.tarea, timeout=6)  # type: ignore[arg-type]

        # Assert: ...y un job posterior no hereda confirmaciones de este.
        assert arnes.broker._launch_acked == set()
        assert arnes.broker._supervised_sent == set()


# ---------------------------------------------------------------------------
# Ancla por AST: la familia de caminos al despacho, enumerada (no muestreada)
# ---------------------------------------------------------------------------

_RAIZ_DEL_PAQUETE = pathlib.Path(__file__).resolve().parents[1] / "sky_claw"


def _nombre_llamado(llamada: ast.Call) -> str | None:
    funcion = llamada.func
    if isinstance(funcion, ast.Name):
        return funcion.id
    if isinstance(funcion, ast.Attribute):
        return funcion.attr
    return None


class _BuscadorDeLlamadas(ast.NodeVisitor):
    """Recorre un árbol una sola vez y anota el ámbito MÁS INTERNO de cada llamada a ``nombre``.

    Sin función que la encierre (módulo, ``if __name__ == "__main__":``, cuerpo de clase) el ámbito es ``<module>``:
    una llamada ahí también salta el fence si nadie la ve.
    """

    def __init__(self, nombre: str) -> None:
        self._nombre = nombre
        self._ambitos = ["<module>"]
        self.encontradas: list[tuple[str, int]] = []

    def _en_funcion(self, nodo: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._ambitos.append(nodo.name)
        self.generic_visit(nodo)
        self._ambitos.pop()

    def visit_FunctionDef(self, nodo: ast.FunctionDef) -> None:  # noqa: N802 - API de ast.NodeVisitor
        self._en_funcion(nodo)

    def visit_AsyncFunctionDef(self, nodo: ast.AsyncFunctionDef) -> None:  # noqa: N802 - API de ast.NodeVisitor
        self._en_funcion(nodo)

    def visit_Call(self, nodo: ast.Call) -> None:  # noqa: N802 - API de ast.NodeVisitor
        if _nombre_llamado(nodo) == self._nombre:
            self.encontradas.append((self._ambitos[-1], nodo.lineno))
        self.generic_visit(nodo)


def _llamadas_en_arbol(arbol: ast.AST, nombre: str) -> list[tuple[str, int]]:
    """``(ámbito más interno, línea)`` de cada llamada a ``nombre`` en ``arbol``, cada una una sola vez."""
    buscador = _BuscadorDeLlamadas(nombre)
    buscador.visit(arbol)
    return buscador.encontradas


def _llamadas_a(nombre: str) -> list[tuple[str, str, int]]:
    """``(archivo relativo, ámbito contenedor, línea)`` de cada llamada a ``nombre`` en ``sky_claw/``."""
    encontradas: list[tuple[str, str, int]] = []
    for ruta in sorted(_RAIZ_DEL_PAQUETE.rglob("*.py")):
        relativa = ruta.relative_to(_RAIZ_DEL_PAQUETE.parent).as_posix()
        for ambito, linea in _llamadas_en_arbol(ast.parse(ruta.read_text(encoding="utf-8")), nombre):
            encontradas.append((relativa, ambito, linea))
    return encontradas


def test_el_enumerador_de_llamadas_ve_las_de_nivel_de_modulo_y_cuenta_cada_una_una_sola_vez() -> None:
    """El ancla promete enumerar TODOS los llamadores: una llamada fuera de toda función no puede escaparle.

    Y una llamada dentro de una función anidada es de la función MÁS INTERNA (una sola vez), no de la externa
    también: contarla doble rompería la igualdad con una lista engañosa.
    """
    # Arrange
    fuente = (
        "execute_worker_manifest(a)\n"  # nivel de módulo: antes invisible
        "def externa():\n"
        "    def interna():\n"
        "        execute_worker_manifest(b)\n"
        "    execute_worker_manifest(c)\n"
        "class K:\n"
        "    execute_worker_manifest(d)\n"  # cuerpo de clase: no es una función
        "    def metodo(self):\n"
        "        self.execute_worker_manifest(e)\n"
        "async def asincrona():\n"
        "    await execute_worker_manifest(f)\n"
        "otra_funcion(g)\n"
    )

    # Act
    llamadas = _llamadas_en_arbol(ast.parse(fuente), "execute_worker_manifest")

    # Assert: (ámbito más interno, línea); ni una de más ni una de menos.
    assert llamadas == [
        ("<module>", 1),
        ("interna", 4),
        ("externa", 5),
        ("<module>", 7),
        ("metodo", 9),
        ("asincrona", 11),
    ]


def test_el_unico_camino_al_despacho_del_worker_pasa_por_la_espera_de_supervision() -> None:
    """Un segundo llamador de ``execute_worker_manifest`` (hermano) saltearía el fence sin romper nada.

    Se enumera, no se muestrea: hoy hay UN llamador, en ``run_worker_session``, y la espera
    de supervisión está ANTES en el mismo cuerpo. Otro llamador, o reordenar las líneas, rompe
    el ancla hasta que alguien decida a conciencia cómo participa del fence.
    """
    # Arrange / Act
    despacho = _llamadas_a("execute_worker_manifest")
    espera = _llamadas_a("_esperar_supervision")

    # Assert
    assert [(a, f) for a, f, _ in despacho] == [("sky_claw/local/mo2/vfs_worker.py", "run_worker_session")]
    assert [(a, f) for a, f, _ in espera] == [("sky_claw/local/mo2/vfs_worker.py", "run_worker_session")]
    assert espera[0][2] < despacho[0][2], "la espera de supervisión debe preceder al despacho"


def test_el_unico_hello_de_worker_declara_la_capacidad_de_fence() -> None:
    """Todo literal ``{"role": "worker", ...}`` del paquete declara ``supervision_fence``."""
    # Arrange / Act
    literales: list[tuple[str, set[str]]] = []
    for ruta in sorted(_RAIZ_DEL_PAQUETE.rglob("*.py")):
        for nodo in ast.walk(ast.parse(ruta.read_text(encoding="utf-8"))):
            if not isinstance(nodo, ast.Dict):
                continue
            pares = {
                clave.value: valor.value
                for clave, valor in zip(nodo.keys, nodo.values, strict=True)
                if isinstance(clave, ast.Constant) and isinstance(valor, ast.Constant)
            }
            if pares.get("role") == "worker":
                claves = {k.value for k in nodo.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                literales.append((ruta.relative_to(_RAIZ_DEL_PAQUETE.parent).as_posix(), claves))

    # Assert
    assert [archivo for archivo, _ in literales] == ["sky_claw/local/mo2/vfs_worker.py"]
    assert "supervision_fence" in literales[0][1]


async def test_camino_feliz_registra_logs_con_timestamp_para_job_result_y_worker_exit(
    tmp_path: pathlib.Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """En el camino feliz, el broker emite logs INFO con timestamp para ``job_result`` recibido y ``worker_exit`` confirmado."""
    import logging

    observado: list[str] = []
    caplog.set_level(logging.INFO, logger="sky_claw.local.mo2.vfs_broker")
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        worker = arnes.worker_real(observado)
        await arnes.esperar_worker_conectado(worker)
        await arnes.launch_ack()
        await asyncio.wait_for(worker, timeout=6)
        await arnes.worker_exit(exit_code=0)
        resultado = await asyncio.wait_for(arnes.tarea, timeout=6)  # type: ignore[arg-type]

    assert resultado.success is True
    eventos = [
        (rec.getMessage(), getattr(rec, "event", None), getattr(rec, "ts_utc", None))
        for rec in caplog.records
        if getattr(rec, "event", None) in ("job_result_received", "worker_exit_confirmed")
    ]
    assert [ev for _, ev, _ in eventos] == ["job_result_received", "worker_exit_confirmed"]
    assert "VFS job_result recibido ts=" in eventos[0][0]
    assert "VFS worker_exit confirmado ts=" in eventos[1][0]
    assert all(isinstance(ts, str) and "T" in ts for _, _, ts in eventos)
