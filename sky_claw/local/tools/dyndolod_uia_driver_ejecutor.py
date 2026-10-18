"""Etapa 9 desatendida — ejecución de los pasos del driver: helper descartable vs hilo.

Mismo diseño que ``dyndolod_uia_ejecutor`` (el gate read-only) y por el mismo
motivo: ``asyncio.wait_for(asyncio.to_thread(...))`` NO cancela un hilo colgado
en una llamada COM, así que el paso productivo corre en un PROCESO descartable
con deadline duro (kill + reap + Job Object). La diferencia que importa acá es
que algunos pasos MUTAN: si el helper muere, se cuelga o devuelve basura
después de recibir un pedido de ``INVOCAR``/``ESCRIBIR_OUTPUT``, este ejecutor
devuelve ``SIN_RESPUESTA`` y el conductor lo trata como desenlace desconocido
(no reintenta).

**Dos ejecutores, y el productivo NO es el cómodo.** ``EjecutorDePasoPorHelper``
lo cablea el composition root; ``EjecutorDePasoEnProceso`` existe para tests y
rig con backends falsos cooperativos (arrastra el defecto del hilo colgado).

**Protocolo.** ``paso.json`` in, ``resultado_paso.json`` out, en un directorio
temporal propio que se borra en toda salida. Archivos y no stdin/stdout: el
ejecutable congelado es una app de subsistema GUI.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys
import tempfile
import time
from collections.abc import Callable
from typing import Any

from sky_claw.app.security.links import rmtree_link_aware
from sky_claw.local.tools._process import assign_kill_on_close_job, close_job, kill_and_reap
from sky_claw.local.tools.dyndolod_uia_driver import (
    NOMBRE_DEL_PEDIDO_DE_PASO,
    NOMBRE_DEL_RESULTADO_DE_PASO,
    BackendDeConduccion,
    ContratoDePasoError,
    EstadoDePaso,
    PedidoDePaso,
    ResultadoDePaso,
    ejecutar_paso_sincrono,
    pedido_de_paso_a_json,
    resultado_de_paso_desde_json,
)
from sky_claw.local.tools.dyndolod_uia_preflight import LocalizadorDeProcesos

logger = logging.getLogger(__name__)

#: Flag con el que el ejecutable CONGELADO se re-invoca como worker del driver.
#: ``sky_claw/__main__.py`` la despacha antes de importar la app. Distinta de la
#: del gate read-only (``--skyclaw-uia-helper``): la superficie mutante es otro
#: proceso, otro módulo y otra puerta de entrada.
FLAG_HELPER_DRIVER_UIA = "--skyclaw-uia-driver-helper"

#: Gracia sobre el deadline del paso. Cubre lo que el paso no mide: arranque del
#: intérprete, ``GetModule`` de UIAutomationCore y la escritura del resultado.
GRACIA_EXTERNA_DEL_PASO_SEGUNDOS = 15.0

#: PyInstaller onefile re-extrae el bundle en cada spawn (mismo valor que el gate).
GRACIA_DE_ARRANQUE_CONGELADO_SEGUNDOS = 90.0

_CREATE_NO_WINDOW = 0x08000000


def comando_del_helper_driver() -> tuple[str, ...]:
    """El argv base del worker mutante en ESTA instalación (fuente o congelado)."""
    if getattr(sys, "frozen", False):
        return (sys.executable, FLAG_HELPER_DRIVER_UIA)
    return (sys.executable, "-m", "sky_claw.local.tools.dyndolod_uia_driver_helper")


def _sin_respuesta(detalle: str) -> ResultadoDePaso:
    return ResultadoDePaso(estado=EstadoDePaso.SIN_RESPUESTA, detalle=detalle)


def _respuesta_pertenece_al_pedido(pedido: PedidoDePaso, resultado: ResultadoDePaso) -> bool:
    """Una instantánea, si viene, tiene que ser del pid pedido. Lo demás lo re-clasifica el conductor."""
    return resultado.instantanea is None or resultado.instantanea.pid == pedido.pid


class EjecutorDePasoPorHelper:
    """Ejecutor PRODUCTIVO: cada paso en un proceso descartable con deadline duro."""

    def __init__(
        self,
        *,
        comando: tuple[str, ...] | None = None,
        gracia_externa_segundos: float = GRACIA_EXTERNA_DEL_PASO_SEGUNDOS,
    ) -> None:
        self._comando = comando
        self._gracia = gracia_externa_segundos

    def _argv(self, directorio: pathlib.Path) -> tuple[str, ...]:
        base = self._comando if self._comando is not None else comando_del_helper_driver()
        return (*base, str(directorio))

    def _plazo(self, pedido: PedidoDePaso) -> float:
        extra = GRACIA_DE_ARRANQUE_CONGELADO_SEGUNDOS if getattr(sys, "frozen", False) else 0.0
        return pedido.timeout_segundos + pedido.intervalo_segundos + self._gracia + extra

    async def ejecutar(self, pedido: PedidoDePaso) -> ResultadoDePaso:
        try:
            directorio = pathlib.Path(tempfile.mkdtemp(prefix="skyclaw-uia-driver-"))
        except OSError as exc:
            return _sin_respuesta(f"no se pudo crear el directorio del canal del driver: {exc}")
        proceso: asyncio.subprocess.Process | None = None
        job: int | None = None
        completado = False
        try:
            try:
                (directorio / NOMBRE_DEL_PEDIDO_DE_PASO).write_text(pedido_de_paso_a_json(pedido), encoding="utf-8")
            except OSError as exc:
                return _sin_respuesta(f"no se pudo escribir el pedido del driver: {exc}")

            kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = _CREATE_NO_WINDOW
            try:
                proceso = await asyncio.create_subprocess_exec(
                    *self._argv(directorio),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    **kwargs,
                )
            except OSError as exc:
                return _sin_respuesta(f"no se pudo lanzar el helper del driver: {exc}")
            job = assign_kill_on_close_job(proceso.pid)

            plazo = self._plazo(pedido)
            try:
                _salida, error = await asyncio.wait_for(proceso.communicate(), timeout=plazo)
                completado = True
            except TimeoutError:
                return _sin_respuesta(
                    f"el helper del driver ({pedido.tipo.value}) excedió su deadline externo ({plazo:.1f}s): "
                    "se termina y se reapea"
                )
            if proceso.returncode != 0:
                cola = error.decode("utf-8", errors="replace")[-400:].strip()
                return _sin_respuesta(f"el helper del driver terminó con código {proceso.returncode}: {cola or '-'}")
            try:
                resultado = resultado_de_paso_desde_json(
                    (directorio / NOMBRE_DEL_RESULTADO_DE_PASO).read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError, ContratoDePasoError) as exc:
                return _sin_respuesta(f"respuesta ilegible del helper del driver: {exc}")
            if not _respuesta_pertenece_al_pedido(pedido, resultado):
                return _sin_respuesta("la instantánea del helper no es del pid pedido")
            return resultado
        finally:
            try:
                if not completado and proceso is not None:
                    await kill_and_reap(proceso)
            finally:
                close_job(job)
                try:
                    rmtree_link_aware(directorio)
                except OSError as exc:
                    logger.warning(
                        "no se pudo borrar el directorio del canal del driver %s: %s",
                        directorio,
                        exc,
                        extra={"operation_type": "dyndolod_uia_driver_canal_sin_limpiar"},
                    )


class EjecutorDePasoEnProceso:
    """Ejecutor de TESTS/RIG: ``ejecutar_paso_sincrono`` en un hilo del pool.

    Honesto sólo con backends falsos cooperativos: una llamada COM real colgada
    bloquearía el hilo para siempre. El censo de wiring exige el helper en todo
    constructor productivo.
    """

    def __init__(
        self,
        *,
        fabrica_backend: Callable[[], BackendDeConduccion],
        localizador: LocalizadorDeProcesos,
        reloj: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], None] = time.sleep,
        gracia_externa_segundos: float = GRACIA_EXTERNA_DEL_PASO_SEGUNDOS,
    ) -> None:
        self._fabrica_backend = fabrica_backend
        self._localizador = localizador
        self._reloj = reloj
        self._dormir = dormir
        self._gracia = gracia_externa_segundos

    async def ejecutar(self, pedido: PedidoDePaso) -> ResultadoDePaso:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    ejecutar_paso_sincrono,
                    pedido,
                    fabrica_backend=self._fabrica_backend,
                    localizador=self._localizador,
                    reloj=self._reloj,
                    dormir=self._dormir,
                ),
                timeout=pedido.timeout_segundos + pedido.intervalo_segundos + self._gracia,
            )
        except TimeoutError:
            return _sin_respuesta("el paso del driver excedió su gracia externa en el hilo del pool (modo tests/rig)")
        except Exception as exc:  # noqa: BLE001 -- paridad con el helper: un crash del paso es SIN_RESPUESTA
            return _sin_respuesta(f"el paso del driver lanzó {type(exc).__name__}: {exc}")
