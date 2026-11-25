"""BodySlide desde el agente LLM como trabajo en segundo plano (PR 2c-2a, R-8 del brief).

**El problema que cierra.** El round de tools del router corta cada llamada a
``DEFAULT_TOOL_ROUND_TIMEOUT`` (120 s) **cancelándola**. Desde 2c-1, una
cancelación con el proceso de BodySlide lanzado deja el journal de la config en
``LANZADO`` (HOLD sin restaurar, hasta tener evidencia de terminalidad). Con la
corrida dentro del round, un build de más de 120 s terminaba siempre en HOLD.

**La forma.** El mismo patrón que la admisión al xEdit Bridge (ADR 0018) y la
etapa 9 (#661): ``run_bodyslide`` lanza un TRABAJO, uno a la vez, y responde
enseguida con su ``trabajo_id``. El resultado llega por la superficie de avisos
del guard (``HITLGuard.notify_operator``).

**Un recurso, una autoridad.** El trabajo no orquesta nada: ejecuta
``run_bodyslide_batch``, que delega en el único ``BodySlidePipelineService``
(lock ``bodyslide-meshes``, journal, cuarentena y move-aside). El gestor solo
decide CUÁNDO corre, nunca CÓMO.

**Apagado.** ``AppContext`` registra :meth:`GestorDeTrabajosDeBodySlide.cerrar`
en su cleanup. Cancelar la task viva sigue la regla de 2c-1: si el proceso ya
estaba lanzado, el journal queda en ``LANZADO`` (HOLD); nunca se restaura a
ciegas.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from sky_claw.app.agent.tools.job_ids import nuevo_id_de_trabajo
from sky_claw.app.agent.tools.system_tools import run_bodyslide_batch

logger = logging.getLogger(__name__)

EstadoDeTrabajo = Literal["en_curso", "exitoso", "fallido", "cancelado"]

#: Cota del aviso de cancelación: corre DURANTE el apagado, y un canal lento no puede retenerlo.
PLAZO_DEL_AVISO_DE_CANCELACION_S = 3.0

#: Lo que ve el agente si la sesión no armó el gestor (fail-closed: sin gestor no hay corrida).
MOTIVO_SIN_GESTOR = (
    "BodySlide no está disponible en esta sesión: corre como trabajo en segundo plano y esta sesión "
    "no tiene gestor de trabajos de BodySlide. No se lanzó nada."
)

#: Largo máximo del detalle de fallo dentro del aviso (el canal remoto no es un log).
_MAXIMO_DETALLE = 600


@dataclass
class TrabajoDeBodySlide:
    """Una corrida de BodySlide lanzada desde el agente LLM."""

    id: str
    parametros: dict[str, Any]
    iniciado_en: float
    estado: EstadoDeTrabajo = "en_curso"
    terminado_en: float | None = None
    resultado: dict[str, Any] | None = None
    tarea: asyncio.Task[None] | None = field(default=None, repr=False)


def _detalle_de_fallo(resultado: dict[str, Any]) -> str:
    detalle = resultado.get("message") or resultado.get("error") or "error sin detalle"
    return str(detalle)[:_MAXIMO_DETALLE]


def _aviso_de_fin(trabajo: TrabajoDeBodySlide) -> str:
    """El aviso que recibe el operador cuando la corrida termina."""
    grupo = trabajo.parametros.get("group") or "-"
    duracion = round(((trabajo.terminado_en or time.time()) - trabajo.iniciado_en) / 60, 1)
    if trabajo.estado == "exitoso":
        return f"✅ BodySlide ({grupo}) terminó en {duracion} min."
    if trabajo.estado == "cancelado":
        return (
            f"⏹️ BodySlide ({grupo}) se canceló tras {duracion} min (Sky-Claw se cerró). Si el proceso ya estaba "
            "lanzado, la config de BodySlide queda en espera hasta tener evidencia de que terminó."
        )
    return f"❌ BodySlide ({grupo}) falló tras {duracion} min: {_detalle_de_fallo(trabajo.resultado or {})}"


def _resultado_desde_json(crudo: str) -> dict[str, Any]:
    """El dict de ``run_bodyslide_batch``. Algo que no sea un objeto JSON es un fallo, nunca un éxito."""
    try:
        datos = json.loads(crudo)
    except (TypeError, ValueError):
        return {"success": False, "message": "la corrida devolvió una respuesta ilegible"}
    if not isinstance(datos, dict):
        return {"success": False, "message": "la corrida devolvió una respuesta que no es un objeto"}
    return datos


class GestorDeTrabajosDeBodySlide:
    """Una corrida de BodySlide a la vez, fuera del round del router, con aviso al terminar.

    ``notificar`` es la superficie de avisos del guard (``HITLGuard.notify_operator``): es
    best-effort y nunca decide el resultado. Lo construye el ``AppContext``, que registra
    :meth:`cerrar` en su cleanup.
    """

    def __init__(self, *, notificar: Callable[[str], Awaitable[None]] | None = None) -> None:
        self._notificar = notificar
        self._actual: TrabajoDeBodySlide | None = None
        # Toda tarea sin terminar, no solo la actual: una corrida queda en estado terminal ANTES de que
        # termine su aviso de fin, y si entra otra en esa ventana, la anterior no puede quedar huérfana.
        self._tareas: set[asyncio.Task[None]] = set()

    @property
    def actual(self) -> TrabajoDeBodySlide | None:
        return self._actual

    def en_curso(self) -> TrabajoDeBodySlide | None:
        """El trabajo vivo, si hay uno."""
        trabajo = self._actual
        if trabajo is None or trabajo.estado != "en_curso":
            return None
        return trabajo

    def iniciar(self, ejecutar: Callable[[], Awaitable[str]], *, parametros: dict[str, Any]) -> dict[str, Any]:
        """Lanza la corrida y responde YA. Sin ``await`` entre el chequeo y la ocupación: no hay carrera."""
        vivo = self.en_curso()
        if vivo is not None:
            return {
                "success": False,
                "message": f"ya hay una corrida de BodySlide en curso ({vivo.id}): esperá su aviso",
                "trabajo_id": vivo.id,
                "estado": vivo.estado,
            }
        trabajo = TrabajoDeBodySlide(
            id=nuevo_id_de_trabajo("bodyslide"),
            parametros=dict(parametros),
            iniciado_en=time.time(),
        )
        trabajo.tarea = asyncio.create_task(self._correr(trabajo, ejecutar), name=f"bodyslide-{trabajo.id}")
        self._tareas.add(trabajo.tarea)
        trabajo.tarea.add_done_callback(self._tareas.discard)
        self._actual = trabajo
        return {
            "success": True,
            "message": "",
            "estado": trabajo.estado,
            "trabajo_id": trabajo.id,
            "instrucciones": (
                "BodySlide corre en segundo plano: Sky-Claw le avisa al operador por este canal cuando termina. "
                "No vuelvas a lanzarlo mientras tanto."
            ),
        }

    async def _correr(self, trabajo: TrabajoDeBodySlide, ejecutar: Callable[[], Awaitable[str]]) -> None:
        try:
            resultado = _resultado_desde_json(await ejecutar())
        except asyncio.CancelledError:
            await self._registrar_cancelacion(trabajo)
            raise
        except Exception as exc:  # noqa: BLE001 -- frontera del trabajo: la tool no debería lanzar
            logger.exception("la corrida de BodySlide (%s) lanzó en vez de devolver su resultado", trabajo.id)
            resultado = {"success": False, "message": f"error inesperado de BodySlide: {exc}"}
        await self._terminar(trabajo, resultado)

    async def _terminar(self, trabajo: TrabajoDeBodySlide, resultado: dict[str, Any]) -> None:
        trabajo.resultado = resultado
        trabajo.estado = "exitoso" if resultado.get("success") is True else "fallido"
        trabajo.terminado_en = time.time()
        await self._avisar(_aviso_de_fin(trabajo))

    async def _registrar_cancelacion(self, trabajo: TrabajoDeBodySlide) -> None:
        """Cierra el trabajo como ``cancelado`` y avisa UNA vez (acotado: corre durante el apagado)."""
        if trabajo.estado != "en_curso":
            return
        trabajo.estado = "cancelado"
        trabajo.terminado_en = time.time()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(self._avisar(_aviso_de_fin(trabajo)), timeout=PLAZO_DEL_AVISO_DE_CANCELACION_S)

    async def _avisar(self, mensaje: str) -> None:
        if self._notificar is None:
            logger.info("BodySlide remoto: %s", mensaje)
            return
        try:
            await self._notificar(mensaje)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- aviso best-effort
            logger.warning("no se pudo avisar al operador del fin de la corrida de BodySlide", exc_info=True)

    async def cerrar(self) -> None:
        """Shutdown: cancela TODA tarea sin terminar (la corrida viva y avisos pendientes) y las espera."""
        pendientes = [tarea for tarea in self._tareas if not tarea.done()]
        for tarea in pendientes:
            tarea.cancel()
        for tarea in pendientes:
            try:
                await tarea
            except asyncio.CancelledError:
                # Se absorbe solo la cancelación pedida acá; si cancelaron a quien cierra, es suya y se propaga.
                actual = asyncio.current_task()
                if actual is not None and actual.cancelling():
                    raise
            except Exception:  # noqa: BLE001 -- shutdown best-effort
                logger.warning("una corrida de BodySlide terminó con error durante el cierre", exc_info=True)
        # Una task cancelada ANTES de entrar a `_correr` nunca ejecuta su handler. No-op si ya cerró.
        if self._actual is not None:
            await self._registrar_cancelacion(self._actual)


async def iniciar_run_bodyslide(
    gestor: GestorDeTrabajosDeBodySlide | None,
    runner: Any,
    *,
    group: str,
    output_path: str,
    preset: str | None,
    build_morphs: bool,
    lock_manager: Any | None,
    snapshot_manager: Any | None,
) -> str:
    """Tool ``run_bodyslide``: lanza la corrida como trabajo y responde con su ``trabajo_id``.

    Sin runner configurado responde enseguida (no hay nada que correr ni que esperar). Sin
    gestor falla cerrada: correr dentro del round del router es justo lo que este módulo evita.
    """
    if runner is None:
        return await run_bodyslide_batch(None, group, output_path, preset, build_morphs)
    if gestor is None:
        return json.dumps({"success": False, "message": MOTIVO_SIN_GESTOR, "error": MOTIVO_SIN_GESTOR})

    async def _ejecutar() -> str:
        return await run_bodyslide_batch(
            runner,
            group,
            output_path,
            preset,
            build_morphs,
            lock_manager=lock_manager,
            snapshot_manager=snapshot_manager,
        )

    parametros = {"group": group, "preset": preset, "build_morphs": build_morphs}
    return json.dumps(gestor.iniciar(_ejecutar, parametros=parametros))


__all__ = [
    "MOTIVO_SIN_GESTOR",
    "GestorDeTrabajosDeBodySlide",
    "TrabajoDeBodySlide",
    "iniciar_run_bodyslide",
]
