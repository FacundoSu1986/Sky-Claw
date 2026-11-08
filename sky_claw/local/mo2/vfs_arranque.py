"""Arranque del worker VFS: prueba del Job ANTES de importar nada pesado (auditoría independiente, H-02).

El problema que cierra. ``SkyClawApp.exe --vfs-worker`` importaba ``vfs_worker`` y, con él, ``uuid`` →
``platform.system()`` → ``cmd.exe /c ver`` (en Windows, la primera llamada lanza ese subproceso). Esos procesos
nacían ANTES de que el worker hubiera probado que vive en el Job del launch: si el proceso que ejecuta Python era
el hijo del bootloader que quedó fuera del Job, sus hijos también nacían fuera.

La propiedad (cuando hay un ``--job-object`` utilizable; ver la excepción más abajo): **mientras la verificación
del Job no haya pasado, solo se carga el conjunto liviano** (este módulo
y ``vfs_job_identity``: stdlib, ``ctypes`` y ``psutil``). ``vfs_worker`` se importa DESPUÉS, y con él todo lo que
dispara ``platform``. Los dos puntos de entrada (el despacho a nivel de módulo de ``sky_claw/__main__.py`` y
``main()``) pasan por :func:`arrancar_worker`: un tercero que no lo hiciera es exactamente "arreglar un hermano y
no al otro" (anclado por AST en ``tests/test_vfs_arranque.py``).

Lo que NO es: este módulo no reemplaza la verificación de ``worker_main``. El worker sigue verificando antes de
abrir un socket, crear un nieto o despachar una herramienta (es también entrada directa, ``python -m
sky_claw.local.mo2.vfs_worker``) y es la única que produce la ``PruebaDeJob`` que exige la sesión. Acá la
verificación se hace una vez más, antes, solo para que el import pesado no ocurra sin contención probada.

``--job-object`` se lee con ``argparse`` (misma semántica que el parser del worker: gana la última aparición,
forma ``--opcion=valor``) pero SIN abreviaturas (el parser del worker tampoco las acepta). Si no hay un
``--job-object`` utilizable se delega en ``worker_main``, que termina con error de uso (64 o 2) antes de abrir nada:
un solo camino para el error de uso. **Excepción declarada:** en ese caso ``vfs_worker`` SE IMPORTA antes del error
de uso, y la entrada directa ``python -m sky_claw.local.mo2.vfs_worker`` también importa antes de verificar; nada se
ejecuta sin verificar, pero esos imports no están cubiertos por la propiedad de arriba.

``vfs_job_identity`` y este módulo tienen sus imports de nivel módulo congelados por igualdad literal: agregar uno
pesado rompe el test, a propósito.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import pathlib
import re
import sys
from collections.abc import Iterator, Sequence
from typing import NoReturn

from sky_claw.local.mo2.vfs_job_identity import (
    JobIdentityError,
    terminar_descendientes_vivos,
    verificar_identidad_del_job,
)

logger = logging.getLogger(__name__)

FLAG_WORKER = "--vfs-worker"
FLAG_NIETO_DE_SONDA = "--vfs-probe-child"
#: Los flags que ``sky_claw/__main__.py`` despacha acá (un test ancla que sus tuplas literales coincidan).
FLAGS_VFS = (FLAG_WORKER, FLAG_NIETO_DE_SONDA)
#: Código de salida del worker que no pudo demostrar su contención en el Job (distinto de 64/70: el bridge lo ve
#: en ``worker_exit``).
EXIT_IDENTIDAD_DEL_JOB = 72

_FORMATO_DEL_REGISTRO = "%(asctime)s %(levelname)s [%(event)s] job_id=%(job_id)s %(message)s"


class _ArgumentosInutilizablesError(Exception):
    """El ``argv`` no permite leer ``--job-object``: se delega el error de uso en ``worker_main``."""


class _ParserDeIdentidad(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _ArgumentosInutilizablesError(message)


def directorio_de_logs() -> pathlib.Path:
    """Repite la ruta de ``logging_config.default_log_dir`` (que no se puede importar acá); un test ancla la igualdad."""
    return pathlib.Path.home() / ".sky_claw" / "logs"


def nombre_de_log_del_worker(job_id: str) -> str:
    """Construye un nombre de log seguro y estable para el job."""
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]+", "-", job_id).strip("-_")
    return f"vfs-worker-{safe_job_id or 'unknown'}.log"


def _identidad_declarada(argv: Sequence[str]) -> tuple[str | None, str | None]:
    """``(job_object, job_id)`` como los entregaría el parser del worker; ``(None, None)`` si no son utilizables."""
    parser = _ParserDeIdentidad(add_help=False, allow_abbrev=False)
    parser.add_argument("--job-object")
    parser.add_argument("--job-id")
    try:
        espacio, _ = parser.parse_known_args(list(argv))
    except _ArgumentosInutilizablesError:
        return None, None
    job_object: str | None = espacio.job_object
    job_id: str | None = espacio.job_id
    return job_object, job_id


def _extra(evento: str, job_id: str | None) -> dict[str, object]:
    return {"event": evento, "operation": "vfs_worker", "job_id": job_id or "-"}


@contextlib.contextmanager
def _registro_de_la_falla(job_id: str | None) -> Iterator[None]:
    """Deja el registro de una falla de identidad SIN ``logging_config`` (importarlo antes de la prueba es el bug).

    Mejor esfuerzo: va a stderr (si existe: un ejecutable de ventana no lo tiene) y al log del job. Que el disco o el
    perfil fallen no puede enmascarar el aborto.
    """
    formato = logging.Formatter(_FORMATO_DEL_REGISTRO)
    manejadores: list[logging.Handler] = []
    aviso: str | None = None
    if sys.stderr is not None:
        manejadores.append(logging.StreamHandler(sys.stderr))
    if job_id:
        try:
            directorio = directorio_de_logs() / "workers"
            directorio.mkdir(parents=True, exist_ok=True)
            manejadores.append(logging.FileHandler(directorio / nombre_de_log_del_worker(job_id), encoding="utf-8"))
        except (OSError, RuntimeError) as exc:  # sin perfil/disco el registro es de mejor esfuerzo
            aviso = f"no se pudo abrir el log del job: {exc}"
    for manejador in manejadores:
        manejador.setFormatter(formato)
        logger.addHandler(manejador)
    try:
        if aviso is not None:
            logger.warning("%s", aviso, extra=_extra("vfs_worker_log_unavailable", job_id))
        yield
    finally:
        for manejador in manejadores:
            logger.removeHandler(manejador)
            manejador.close()


def abortar_por_identidad(job_id: str | None, causa: JobIdentityError) -> int:
    """El worker no pudo probar su contención: intenta no dejar procesos propios corriendo y sale con 72.

    Es la ÚNICA implementación del aborto: la usan el arranque liviano y ``worker_main`` (dos superficies, un
    recurso). La terminación es un intento: si algún descendiente no muere se registra ``CRITICAL`` y se sale igual
    con 72 (sin abrir un socket ni autorizar nada); ese proceso puede quedar vivo.
    """
    with _registro_de_la_falla(job_id):
        logger.error(
            "VFS worker sin identidad de Job verificada: %s",
            causa,
            extra=_extra("vfs_worker_job_identity_failed", job_id),
        )
        try:
            terminados = terminar_descendientes_vivos()
        except JobIdentityError as exc:
            logger.critical(
                "VFS worker no pudo terminar a sus descendientes tras fallar la identidad del Job: %s",
                exc,
                extra=_extra("vfs_worker_cleanup_failed", job_id),
            )
        else:
            if terminados:
                logger.warning(
                    "VFS worker terminó descendientes propios sin contención: %s",
                    list(terminados),
                    extra=_extra("vfs_worker_descendants_terminated", job_id),
                )
    return EXIT_IDENTIDAD_DEL_JOB


def arrancar_worker(flag: str, argv: Sequence[str]) -> int:
    """Punto de entrada común de ``--vfs-worker`` y ``--vfs-probe-child``: verifica el Job y recién entonces importa.

    ``argv`` es lo que sigue al flag. Devuelve el código de salida del proceso.
    """
    if flag not in FLAGS_VFS:
        raise ValueError(f"flag de arranque VFS desconocido: {flag}")
    worker_argv = list(argv)
    if flag == FLAG_NIETO_DE_SONDA:
        # El nieto de sonda ya nace DENTRO del Job (es hijo del worker verificado) y no recibe ``--job-object``.
        worker_argv = ["--probe-child", *worker_argv]
    else:
        job_object, job_id = _identidad_declarada(worker_argv)
        if job_object:
            try:
                verificar_identidad_del_job(job_object)
            except JobIdentityError as exc:
                return abortar_por_identidad(job_id, exc)
    from sky_claw.local.mo2 import vfs_worker  # noqa: PLC0415 - el import pesado va DESPUÉS de la prueba

    return vfs_worker.worker_main(worker_argv)
