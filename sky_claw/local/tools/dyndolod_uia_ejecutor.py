"""T5-v2.1 — ejecución del gate UIA: helper descartable vs hilo del pool.

**El defecto que este módulo cierra.** ``asyncio.wait_for(asyncio.to_thread(...))``
NO cancela el hilo subyacente: una llamada COM de UI Automation que nunca
retorna deja ese hilo del pool bloqueado para siempre, y la "gracia externa"
sólo decide cuánto espera el padre — no limpia nada. El gate productivo exige
una cota que sea real también cuando el sensor se cuelga dentro de una llamada
del sistema, así que la observación entera corre en un PROCESO descartable:

    padre → spawn helper → helper inicializa COM → observa →
    escribe el resultado serializable → helper termina

y el timeout del padre es duro:

    timeout → terminate/kill del helper → reap → ``UIA_NO_DISPONIBLE`` →
    el runner corta fail-closed

**Un proceso, no un hilo.** ``kill_and_reap`` sobre el helper (taskkill /T en
Windows, ``proc.kill()`` + ``wait`` en todos lados) sí termina el trabajo
bloqueado, porque el sistema operativo no le pide permiso al hilo: lo termina.
No existe "matar un thread de Python" portátil y este módulo no lo intenta.

**Dos ejecutores, y el productivo NO es el cómodo.** ``EjecutorGatePorHelper``
es el de producción (lo cablea el composition root). ``EjecutorGateEnProceso``
existe para los tests y el rig: corre el mismo ``ejecutar_gate_sincrono`` en un
hilo con observadores falsos, y **arrastra el defecto original** — un fake
cooperativo no se cuelga, una llamada COM real sí. Su docstring lo dice y el
censo de wiring exige el helper en TODO constructor productivo.

**Protocolo del helper.** El payload viaja por ARCHIVOS dentro de un directorio
temporal (``pedido.json`` in, ``resultado.json`` out) y no por
stdin/stdout: el ejecutable congelado de Windows es una app de subsistema GUI
(``console=False``) donde ``sys.stdout`` puede no existir, y un contrato que
sólo funciona desde la fuente no sirve para el producto que se distribuye. El
directorio se borra en TODA salida, después de matar y reapear al helper si
seguía vivo.
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
from sky_claw.local.tools.dyndolod_uia_gate import (
    GRACIA_EXTERNA_DEL_GATE_SEGUNDOS,
    NOMBRE_DEL_PEDIDO,
    NOMBRE_DEL_RESULTADO,
    RAZONES_TRANSITORIAS_POR_POLITICA,
    ContratoDeHelperError,
    PedidoDeGate,
    PoliticaDeReintento,
    ejecutar_gate_sincrono,
    pedido_a_json,
    resultado_desde_json,
    resultado_sin_backend,
)
from sky_claw.local.tools.dyndolod_uia_preflight import (
    EstadoPreflight,
    LocalizadorDeProcesos,
    ObservacionUIAError,
    ObservadorUIA,
    ResultadoPreflightUIA,
    SolicitudPreflightUIA,
    canonicalizar_ruta_windows,
)

logger = logging.getLogger(__name__)

#: Flag con el que el ejecutable CONGELADO se re-invoca a sí mismo como helper.
#: ``sky_claw/__main__.py`` la despacha ANTES de importar la app (NiceGUI,
#: Web, etc.), así que el helper arranca sin arrastrar la GUI. En modo fuente el
#: helper se lanza con ``-m`` y este flag no participa.
FLAG_HELPER_UIA = "--skyclaw-uia-helper"

#: Gracia extra sobre el deadline cuando el binario corre congelado: PyInstaller
#: onefile re-extrae el bundle en CADA spawn, y el plazo del padre tiene que
#: cubrir extracción + gate, no sólo el gate. Sin esto, un arranque en frío
#: mataría al helper antes de que llegue a observar.
GRACIA_DE_ARRANQUE_CONGELADO_SEGUNDOS = 90.0

#: Windows ``CREATE_NO_WINDOW``: el helper es un proceso de consola y no debe
#: parpadear una ventana (en modo congelado hereda el subsistema GUI, pero el
#: flag es inocuo).
_CREATE_NO_WINDOW = 0x08000000


def comando_del_helper() -> tuple[str, ...]:
    """El argv base para invocar el helper en ESTA instalación.

    Modo fuente: el mismo intérprete con ``-m``. Modo congelado
    (``sys.frozen``): el propio ejecutable con :data:`FLAG_HELPER_UIA`, porque
    un binario PyInstaller no puede correr ``-m`` de un paquete embebido.
    La elección vive acá y no en cada call site.
    """
    if getattr(sys, "frozen", False):
        return (sys.executable, FLAG_HELPER_UIA)
    return (sys.executable, "-m", "sky_claw.local.tools.dyndolod_uia_helper")


def _sin_respuesta(solicitud: SolicitudPreflightUIA, detalle: str) -> ResultadoPreflightUIA:
    """El fail-closed del ejecutor: el sensor no respondió, el pipeline no decide."""
    return resultado_sin_backend(solicitud, ObservacionUIAError(detalle))


def _salida_esperada_coincide(solicitud: SolicitudPreflightUIA, valor_esperado: str) -> bool:
    """``valor_esperado`` del helper vs ``salida_administrada_esperada`` de la solicitud.

    Usa la canonicalización textual ya existente; no inventa una segunda
    normalización. Dos escrituras distintas de la MISMA ruta Win32 coinciden;
    si alguna no canonicaliza, sólo queda la igualdad literal.
    """
    if valor_esperado == solicitud.salida_administrada_esperada:
        return True
    esperado = canonicalizar_ruta_windows(solicitud.salida_administrada_esperada)
    eco = canonicalizar_ruta_windows(valor_esperado)
    return esperado is not None and eco is not None and esperado == eco


def _respuesta_pertenece_a_la_solicitud(solicitud: SolicitudPreflightUIA, resultado: ResultadoPreflightUIA) -> bool:
    """La respuesta internamente válida es de ESTA solicitud, no de otra.

    Un veredicto CONCLUYENTE (``MATCH``/``MISMATCH``) con ``solicitud.pid``
    definido exige eco exacto: ``resultado.pid`` tiene que existir y ser el
    mismo. Un ``UNKNOWN`` temprano puede no haber observado pid; si no lo
    declara, no se imputa identidad ajena. Si el UNKNOWN sí declara pid y
    no coincide, sí es de otra solicitud.
    """
    if resultado.tool != solicitud.tool:
        return False
    if solicitud.pid is not None:
        if resultado.estado in {EstadoPreflight.MATCH, EstadoPreflight.MISMATCH}:
            if resultado.pid != solicitud.pid:
                return False
        elif resultado.pid is not None and resultado.pid != solicitud.pid:
            return False
    return _salida_esperada_coincide(solicitud, resultado.valor_esperado)


class EjecutorGatePorHelper:
    """Ejecutor PRODUCTIVO: la observación corre en un proceso descartable.

    El directorio temporal es el canal de datos; el proceso, la unidad
    descartable. Toda salida —veredicto, timeout, excepción del helper o
    cancelación del caller— mata y reapea al helper si seguía vivo, cierra el
    Job Object que lo retiene y borra el directorio. No queda nada pendiente del
    mecanismo, y por eso el test de hard-hang puede exigirlo.
    """

    def __init__(
        self,
        *,
        comando: tuple[str, ...] | None = None,
        gracia_externa_segundos: float = GRACIA_EXTERNA_DEL_GATE_SEGUNDOS,
    ) -> None:
        self._comando = comando
        self._gracia = gracia_externa_segundos

    def _argv(self, directorio: pathlib.Path) -> tuple[str, ...]:
        base = self._comando if self._comando is not None else comando_del_helper()
        return (*base, str(directorio))

    def _plazo(self, timeout_segundos: float, intervalo_segundos: float) -> float:
        extra = GRACIA_DE_ARRANQUE_CONGELADO_SEGUNDOS if getattr(sys, "frozen", False) else 0.0
        return timeout_segundos + intervalo_segundos + self._gracia + extra

    async def ejecutar(
        self,
        solicitud: SolicitudPreflightUIA,
        *,
        politica: PoliticaDeReintento,
        timeout_segundos: float,
        intervalo_segundos: float,
    ) -> ResultadoPreflightUIA:
        # El setup del canal también respeta el contrato del puerto: un fallo de
        # `mkdtemp`/`write_text` (permisos, disco lleno) se sintetiza como
        # fail-closed y NUNCA escapa como OSError — el ejecutor no puede romper
        # al runner por un problema del andamiaje.
        try:
            directorio = pathlib.Path(tempfile.mkdtemp(prefix="skyclaw-uia-helper-"))
        except OSError as exc:
            return _sin_respuesta(solicitud, f"no se pudo crear el directorio del canal UIA: {exc}")
        proceso: asyncio.subprocess.Process | None = None
        job: int | None = None
        completado = False
        try:
            # El pedido entero viaja por archivo: solicitud + política + plazos.
            try:
                (directorio / NOMBRE_DEL_PEDIDO).write_text(
                    pedido_a_json(
                        PedidoDeGate(
                            solicitud=solicitud,
                            politica=politica,
                            timeout_segundos=timeout_segundos,
                            intervalo_segundos=intervalo_segundos,
                        )
                    ),
                    encoding="utf-8",
                )
            except OSError as exc:
                return _sin_respuesta(solicitud, f"no se pudo escribir el pedido del helper UIA: {exc}")

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
                return _sin_respuesta(solicitud, f"no se pudo lanzar el helper UIA: {exc}")

            # U-02: cuando assign_kill_on_close_job pudo crear/asignar el Job
            # Object, KILL_ON_JOB_CLOSE añade protección frente a muerte dura
            # del padre (SIGKILL/corte de handles) mientras el helper está
            # colgado en COM: el `finally` no corre en esa muerte, el job sí.
            # Si devuelve None (POSIX o fallo Win32) no hay esa capa extra;
            # la garantía dura sigue siendo kill_and_reap.
            job = assign_kill_on_close_job(proceso.pid)

            plazo = self._plazo(timeout_segundos, intervalo_segundos)
            try:
                _salida, error = await asyncio.wait_for(proceso.communicate(), timeout=plazo)
                completado = True
            except TimeoutError:
                return _sin_respuesta(
                    solicitud,
                    f"el helper UIA de Output excedió su deadline externo ({plazo:.1f}s): "
                    "se termina y se reapea (una llamada COM colgada no puede sobrevivir al gate)",
                )

            if proceso.returncode != 0:
                cola = error.decode("utf-8", errors="replace")[-400:].strip()
                return _sin_respuesta(
                    solicitud,
                    f"el helper UIA terminó con código {proceso.returncode}: {cola or 'sin stderr'}",
                )

            try:
                resultado = resultado_desde_json((directorio / NOMBRE_DEL_RESULTADO).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ContratoDeHelperError) as exc:
                return _sin_respuesta(solicitud, f"respuesta ilegible del helper UIA: {exc}")
            if not _respuesta_pertenece_a_la_solicitud(solicitud, resultado):
                return _sin_respuesta(
                    solicitud,
                    "la respuesta del helper UIA no corresponde a la solicitud enviada "
                    f"(tool={resultado.tool!r} pid={resultado.pid} "
                    f"valor_esperado={resultado.valor_esperado!r})",
                )
            return resultado
        finally:
            try:
                if not completado and proceso is not None:
                    # El kill es del PROCESO: incluye el caso "colgado dentro de
                    # una llamada COM que nunca retorna", que ningún mecanismo
                    # de hilos de Python puede interrumpir.
                    await kill_and_reap(proceso)
            finally:
                close_job(job)
                # El directorio del canal es PROPIO (lo creó `mkdtemp`), pero se
                # borra con la primitiva link-aware como todo el paquete: un
                # junction plantado dentro a mitad de la observación no debe
                # llevarse su destino. El fallo de limpieza no puede pisar el
                # veredicto ya decidido, pero TAMPOCO se esconde: se loguea —un
                # directorio que no se pudo borrar (lock de AV, permisos) es
                # operativo y acumula datos de la corrida en disco.
                try:
                    rmtree_link_aware(directorio)
                except OSError as exc:
                    logger.warning(
                        "no se pudo borrar el directorio del canal UIA %s: %s",
                        directorio,
                        exc,
                        extra={"operation_type": "dyndolod_uia_canal_sin_limpiar"},
                    )


class EjecutorGateEnProceso:
    """Ejecutor de TESTS/RIG: ``ejecutar_gate_sincrono`` en un hilo del pool.

    **No es el productivo y no puede serlo.** ``wait_for`` acota la ESPERA,
    pero cancelar el await de ``to_thread`` no interrumpe el hilo: con una
    llamada COM real colgada, el hilo del pool queda bloqueado hasta que el
    llamado del sistema retorne (o para siempre). Es aceptable acá porque sus
    observadores son falsos y cooperativos — el observador de guion retorna
    siempre — y porque el rig ejercita el camino real por el helper.

    Mantiene la misma firma y el mismo contrato de resultados que el ejecutor
    productivo: el runner no puede distinguirlos (ni debería).
    """

    def __init__(
        self,
        *,
        fabrica_observador: Callable[[], ObservadorUIA],
        localizador: LocalizadorDeProcesos,
        reloj: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], None] = time.sleep,
        gracia_externa_segundos: float = GRACIA_EXTERNA_DEL_GATE_SEGUNDOS,
    ) -> None:
        self._fabrica_observador = fabrica_observador
        self._localizador = localizador
        self._reloj = reloj
        self._dormir = dormir
        self._gracia = gracia_externa_segundos

    async def ejecutar(
        self,
        solicitud: SolicitudPreflightUIA,
        *,
        politica: PoliticaDeReintento,
        timeout_segundos: float,
        intervalo_segundos: float,
    ) -> ResultadoPreflightUIA:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    ejecutar_gate_sincrono,
                    solicitud,
                    fabrica_observador=self._fabrica_observador,
                    localizador=self._localizador,
                    timeout_segundos=timeout_segundos,
                    intervalo_segundos=intervalo_segundos,
                    reloj=self._reloj,
                    dormir=self._dormir,
                    razones_transitorias=RAZONES_TRANSITORIAS_POR_POLITICA[politica],
                ),
                timeout=timeout_segundos + intervalo_segundos + self._gracia,
            )
        except TimeoutError:
            return _sin_respuesta(
                solicitud,
                "el gate UIA de Output excedió su gracia externa: la llamada COM quedó bloqueada "
                "en el hilo del pool (modo de tests/rig; el productivo usa el helper descartable)",
            )
