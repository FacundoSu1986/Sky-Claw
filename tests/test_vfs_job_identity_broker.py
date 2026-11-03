"""Identidad del Job en el broker (P2): sin la identidad correcta no hay ``supervised`` ni herramienta.

El fence de #856 prueba «el bridge asignó el proceso que lanzó MO2». P2 le agrega QUIÉN es el Job: el broker
elige un nombre inadivinable por launch, el bridge lo crea y lo confirma en el ``launch_ack``, y el worker
efectivo lo verifica y lo declara en su ``hello``. El broker solo autoriza si LAS DOS confirmaciones nombran
el Job que él pidió.

Propiedad del mecanismo (no un recordatorio): **el nombre del Job nace en ``_registrar_job`` —el único punto
por el que pasan ``submit`` y ``open_session``— y es lo único que el broker acepta en el ``launch_ack`` y en
el ``hello``.** Dos superficies, un recurso: ambas quedan cubiertas por construcción, y un test parametrizado
sobre las herramientas lo comprueba.

Los tests ejercen el broker REAL; el bridge y el worker "crudos" hablan el protocolo a mano para poder mentir.
La verificación nativa de Windows vive en ``test_vfs_job_identity_windows.py``.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from sky_claw.local.mo2.vfs_contracts import ALLOWED_VFS_SESSION_TOOL_IDS, ALLOWED_VFS_TOOL_IDS
from sky_claw.local.mo2.vfs_job_identity import generar_nombre_de_job, validar_nombre_de_job
from tests.test_vfs_fence_de_supervision import _Arnes, _hasta

#: Mismas herramientas que ``test_toda_herramienta_del_broker_espera_la_misma_confirmacion``.
_HERRAMIENTAS = sorted(set(ALLOWED_VFS_TOOL_IDS) | set(ALLOWED_VFS_SESSION_TOOL_IDS))


@pytest.mark.parametrize("tool_id", _HERRAMIENTAS)
async def test_toda_herramienta_pide_un_job_con_nombre_valido(tmp_path: pathlib.Path, tool_id: str) -> None:
    # Arrange / Act: ``submit`` (health, loot_sort) y ``open_session`` (texgen, dyndolod) por igual.
    async with _Arnes(tmp_path, tool_id=tool_id) as arnes:
        lanzamiento = await arnes.lanzar_job()

        # Assert
        assert validar_nombre_de_job(lanzamiento["job_object"]) == lanzamiento["job_object"]
        assert arnes.broker._job_objects[arnes.job_id] == lanzamiento["job_object"]


async def test_cada_launch_recibe_un_nombre_distinto(tmp_path: pathlib.Path) -> None:
    # Arrange / Act: dos launches sucesivos del mismo broker.
    nombres: list[str] = []
    for repeticion in range(2):
        async with _Arnes(tmp_path / f"r{repeticion}") as arnes:
            nombres.append((await arnes.lanzar_job())["job_object"])
    # Assert: un nombre reutilizado dejaría a un launch heredar el Job (o su squatter) del anterior.
    assert len(set(nombres)) == 2


async def test_el_worker_y_el_ack_con_la_identidad_correcta_son_autorizados(tmp_path: pathlib.Path) -> None:
    # Arrange
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        crudo = await arnes.worker_crudo()
        assert (await crudo.leer(timeout=5))["type"] == "hello_ack"  # type: ignore[index]

        # Act
        await arnes.launch_ack()
        autorizacion = await crudo.leer(timeout=5)

        # Assert
        assert autorizacion is not None and autorizacion["type"] == "supervised"


@pytest.mark.parametrize(
    "identidad",
    [
        pytest.param(None, id="sin_job_object"),
        pytest.param(generar_nombre_de_job(), id="job_de_otro_launch"),
        pytest.param("Global\\SkyClawVfs-" + "0" * 32, id="otro_espacio_de_nombres"),
        pytest.param("", id="vacio"),
        pytest.param(12345, id="no_es_un_string"),
    ],
)
async def test_un_worker_con_otra_identidad_es_rechazado_y_nunca_se_autoriza(
    tmp_path: pathlib.Path, identidad: object
) -> None:
    # Arrange
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        mentiroso = await arnes.worker_crudo(job_object=identidad)

        # Act: aunque el bridge confirme correctamente, el worker no declaró el Job que se pidió.
        respuesta = await mentiroso.leer(timeout=2)
        await arnes.launch_ack()
        despues_del_ack = await mentiroso.leer()

        # Assert: ni hello_ack ni supervised, y el broker no lo registra como el worker del job.
        assert respuesta is None
        assert despues_del_ack is None
        assert arnes.job_id not in arnes.broker._worker_by_job


@pytest.mark.parametrize(
    "identidad",
    [
        pytest.param(None, id="bridge_viejo_sin_job_object"),
        pytest.param(generar_nombre_de_job(), id="el_bridge_creo_otro_job"),
        pytest.param("", id="vacio"),
    ],
)
async def test_un_ack_sin_la_identidad_pedida_no_autoriza_al_worker(tmp_path: pathlib.Path, identidad: object) -> None:
    # Arrange: el worker es correcto; lo que falla es la confirmación del bridge.
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        crudo = await arnes.worker_crudo()
        assert (await crudo.leer(timeout=5))["type"] == "hello_ack"  # type: ignore[index]

        # Act
        await arnes.launch_ack(job_object=identidad)
        despues_del_ack = await crudo.leer()

        # Assert: no queda registrado como confirmado, no se envía `supervised` y se corta al worker.
        assert despues_del_ack is None
        assert arnes.job_id not in arnes.broker._launch_acked
        assert arnes.job_id not in arnes.broker._supervised_sent


async def test_un_ack_correcto_posterior_a_uno_con_identidad_ajena_no_resucita_la_autorizacion(
    tmp_path: pathlib.Path,
) -> None:
    # Arrange
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        crudo = await arnes.worker_crudo()
        assert (await crudo.leer(timeout=5))["type"] == "hello_ack"  # type: ignore[index]

        # Act: primero una confirmación con otro Job (denegada → se corta al worker), luego la correcta.
        await arnes.launch_ack(job_object=generar_nombre_de_job())
        await _hasta(lambda: arnes.job_id not in arnes.broker._worker_by_job)
        await arnes.launch_ack()
        await asyncio.sleep(0.2)

        # Assert: el worker ya fue cortado; nada se le puede entregar por ese socket.
        assert await crudo.leer() is None
        assert arnes.job_id not in arnes.broker._supervised_sent


async def test_el_nombre_del_job_se_limpia_cuando_el_job_termina(tmp_path: pathlib.Path) -> None:
    # Arrange
    observado: list[str] = []
    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        worker = arnes.worker_real(observado)
        await arnes.esperar_worker_conectado(worker)
        await arnes.launch_ack()

        # Act
        await asyncio.wait_for(worker, timeout=6)
        await arnes.worker_exit()
        await asyncio.wait_for(arnes.tarea, timeout=6)  # type: ignore[arg-type]

        # Assert
        assert arnes.job_id not in arnes.broker._job_objects


async def test_un_worker_cuya_prueba_nombra_otro_job_no_ejecuta_la_herramienta(tmp_path: pathlib.Path) -> None:
    """El worker REAL (``run_worker_session``) con una prueba de otro Job: el broker lo rechaza → cero herramientas."""
    # Arrange
    from sky_claw.local.mo2.vfs_job_identity import PruebaDeJob
    from sky_claw.local.mo2.vfs_worker import VfsToolExecution, run_worker_session

    observado: list[str] = []

    async def handler(_manifiesto: object) -> VfsToolExecution:
        observado.append("handler")
        return VfsToolExecution.ok()

    async with _Arnes(tmp_path) as arnes:
        await arnes.lanzar_job()
        ajena = PruebaDeJob(nombre=generar_nombre_de_job(), pid=1, procesos_totales=1, procesos_activos=1)
        worker = asyncio.create_task(
            run_worker_session(
                manifest_path=arnes.broker._jobs_dir / f"{arnes.job_id}.json",
                descriptor_path=arnes.broker.descriptor_path,
                expected_job_id=arnes.job_id,
                handlers={"health": handler},  # type: ignore[dict-item]
                supervision_timeout=1.0,
                job_identity=ajena,
            )
        )
        arnes._auxiliares.append(worker)

        # Act
        await arnes.launch_ack()
        with pytest.raises(Exception):  # noqa: B017, PT011 - cualquier fallo cerrado del worker es válido; lo que importa es el efecto
            await asyncio.wait_for(worker, timeout=6)

        # Assert
        assert observado == [], "la herramienta no puede correr sin la identidad pedida por el broker"
