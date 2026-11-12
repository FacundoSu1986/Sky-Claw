"""Tools LLM del xEdit Bridge (ADR 0018): auditoría nativa de plugins con SSEEdit.

Hermanas GUI: ``sky_claw/app/orchestrator/tool_strategies/auditar_plugin_nativo.py`` y
``admitir_plugin_nativo.py``. Las dos superficies delegan en el MISMO ``XEditBridgeService``; acá
vive lo propio de la capa del agente: su aprobación humana (no tiene un middleware HITL general,
#217) y el plazo del router.

**El plazo del router.** El round de tools corta cada llamada a ``DEFAULT_TOOL_ROUND_TIMEOUT``
(120 s) CANCELÁNDOLA. Por eso, en esta superficie:

* ``auditar_plugin_nativo`` es de lectura y nunca pide aprobación. Corre con un plazo TOTAL que
  vence antes que el round (motor ≤ ``TIMEOUT_MAXIMO_LLM`` + ``MARGEN_LLM_S``): si el bridge no
  termina, el error es legible ("tiempo límite"), nunca una cancelación muda. Una corrida real tarda
  10-20 s.
* ``admitir_plugin_nativo`` espera una decisión humana y después copia (hasta 900 s), así que es un
  TRABAJO en segundo plano, como ``generate_lods``: responde enseguida y el pedido de aprobación y el
  resultado viajan por la superficie de avisos del ``HITLGuard`` (Telegram). Nunca espera una
  decisión humana dentro del round. Sin ``HITLGuard`` se rechaza, y solo ``APPROVED`` copia.

**Resultado acotado.** El router recorta el resultado a ``MAX_TOOL_RESULT_CHARS`` POR POSICIÓN, lo que
rompería el JSON a mitad de los hallazgos: el handler poda hallazgos (y después fallas) hasta que
entre en ``PRESUPUESTO_RESULTADO_LLM``, y suma los podados a sus contadores de omitidos.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from sky_claw.app.agent.tools.schemas import TIMEOUT_MAXIMO_LLM
from sky_claw.app.security.hitl import Decision, new_hitl_request_id
from sky_claw.app.security.sanitize import sanitize_for_prompt
from sky_claw.local.tools.xedit_bridge_client import nombre_visible, validar_plugin

if TYPE_CHECKING:
    from sky_claw.app.security.hitl import HITLGuard
    from sky_claw.local.tools.xedit_bridge_service import XEditBridgeService

logger = logging.getLogger(__name__)

#: Margen del agente sobre el timeout del motor: el bridge tarda pocos segundos fuera del motor
#: (gate de integridad, arranque y cierre), y la suma tiene que vencer antes que el round del router.
MARGEN_LLM_S = 30.0
#: Presupuesto del JSON de una auditoría, por debajo del recorte del router (``MAX_TOOL_RESULT_CHARS``).
PRESUPUESTO_RESULTADO_LLM = 3500
#: Cota del aviso de cancelación: corre durante el apagado de la app.
PLAZO_DEL_AVISO_DE_CANCELACION_S = 3.0

_SIN_SERVICIO = "xEdit Bridge no disponible: definí XEDIT_BRIDGE_ROOT con la carpeta BridgeTrabajo de la instalación"
_SIN_GESTOR = (
    "La admisión no está disponible en esta sesión: falta el gestor de trabajos en segundo plano que "
    "Sky-Claw arma al iniciar el agente. No se copió nada."
)
_PODABLES = (("hallazgos", "hallazgos_omitidos"), ("fallas", "fallas_omitidas"))

EstadoDeAdmision = Literal["esperando_aprobacion", "en_curso", "exitosa", "fallida", "rechazada", "cancelada"]
_ESTADOS_VIVOS = frozenset({"esperando_aprobacion", "en_curso"})
_IDS = itertools.count(1)


def _fallo(mensaje: str, **extra: Any) -> str:
    return json.dumps({"success": False, "message": sanitize_for_prompt(mensaje, max_length=300), **extra})


def _nombre(plugin: str) -> str:
    return sanitize_for_prompt(plugin, max_length=255)


def _json_acotado(resultado: dict[str, Any], limite: int = PRESUPUESTO_RESULTADO_LLM) -> str:
    """JSON VÁLIDO de ``resultado`` que entra en ``limite`` caracteres.

    Poda del final (los hallazgos y las fallas ya llegan acotados y en el orden del bridge) y suma lo
    podado al contador de omitidos: el modelo sabe cuántos faltan y conserva ``log_path`` para el log
    completo. Si ni así entra, falla cerrado: un veredicto que no se puede mostrar entero no aprueba.
    """
    resultado = dict(resultado)
    texto = json.dumps(resultado)
    for campo, contador in _PODABLES:
        items = resultado.get(campo)
        items = list(items) if isinstance(items, list) else []
        while len(texto) > limite and items:
            items.pop()
            resultado[campo] = items
            resultado[contador] = int(resultado.get(contador) or 0) + 1
            texto = json.dumps(resultado)
    if len(texto) > limite:
        mensaje = "el resultado de la auditoría nativa no entra en la respuesta: revisá el log del bridge"
        log_path = sanitize_for_prompt(str(resultado.get("log_path") or ""), max_length=500)
        fallo = _fallo(mensaje, aprobado=False, log_path=log_path)
        # Se mide DESPUÉS de serializar: json.dumps escapa lo no-ASCII (un emoji ocupa 12 caracteres).
        return fallo if len(fallo) <= limite else _fallo(mensaje, aprobado=False)
    return texto


async def auditar_plugin_nativo(
    service: XEditBridgeService | None,
    plugin: str,
    timeout: int | None = None,
    *,
    admisiones: GestorDeAdmisionesNativas | None = None,
) -> str:
    """Chequeo nativo de SSEEdit sobre la copia admitida de ``plugin`` (read-only)."""
    if service is None:
        return _fallo(_SIN_SERVICIO, aprobado=False)
    trabajo = admisiones.en_curso(plugin) if admisiones is not None else None
    if trabajo is not None:
        # Auditar ahora daría "no admitido" o la copia anterior: se dice lo que pasa.
        return _fallo(
            f"la admisión de {_nombre(plugin)} sigue en curso ({trabajo.id}, {trabajo.estado}): auditalo "
            "cuando llegue el aviso de que terminó",
            aprobado=False,
            admision_en_curso=trabajo.id,
        )
    # Defensa en profundidad: el esquema ya lo acota, pero el handler nunca pide más de lo que el round aguanta.
    motor = min(timeout, TIMEOUT_MAXIMO_LLM) if timeout is not None else None
    return _json_acotado(await service.auditar(plugin, timeout=motor, margen_s=MARGEN_LLM_S))


async def admitir_plugin_nativo(
    service: XEditBridgeService | None,
    hitl: HITLGuard | None,
    plugin: str,
    *,
    admisiones: GestorDeAdmisionesNativas | None = None,
) -> str:
    """Lanza la admisión de ``plugin`` como trabajo en segundo plano; copia solo con aprobación humana."""
    if service is None:
        return _fallo(_SIN_SERVICIO)
    if hitl is None:
        return _fallo("La admisión requiere aprobación humana y no hay HITLGuard configurado: no se copió nada.")
    if admisiones is None:
        return _fallo(_SIN_GESTOR)
    try:
        return json.dumps(admisiones.iniciar(service, hitl, plugin))
    except ValueError as exc:
        return _fallo(f"no se lanzó la admisión: {exc}")


async def _pedir_aprobacion_de_admision(hitl: HITLGuard, visible: str, perfil: str) -> Decision:
    """Pedido de aprobación de la admisión: el nombre ENTERO y como lo ve el operador.

    Misma categoría que el gate de la GUI (``tool_execution``): las dos superficies piden lo mismo y
    solo el «Modo local», elegido por el operador, la auto-aprueba.
    """
    return await hitl.request_approval(
        request_id=new_hitl_request_id("xedit-bridge-admit"),
        reason=f"Admitir {visible} (perfil MO2 «{perfil}») en el laboratorio del xEdit Bridge",
        detail=(
            "Copia el plugin y sus masters, resueltos con la precedencia de MO2, a la carpeta "
            "Admitidos del bridge. MO2 y el juego solo se leen; no se modifica ningún archivo suyo."
        ),
        category="tool_execution",
    )


@dataclass
class TrabajoDeAdmision:
    """Una admisión lanzada desde el agente LLM."""

    id: str
    plugin: str
    visible: str
    iniciado_en: float
    estado: EstadoDeAdmision = "esperando_aprobacion"
    terminado_en: float | None = None
    resultado: dict[str, Any] | None = None
    tarea: asyncio.Task[None] | None = field(default=None, repr=False)


def _aviso_de_fin(trabajo: TrabajoDeAdmision) -> str:
    """El aviso que recibe el operador por el canal del guard cuando la admisión termina."""
    resultado = trabajo.resultado or {}
    if trabajo.estado == "exitosa":
        return (
            f"✅ {trabajo.visible} quedó admitido en el xEdit Bridge ({resultado.get('admission_id') or '-'}): "
            "ya se puede auditar con auditar_plugin_nativo."
        )
    if trabajo.estado == "rechazada":
        return f"Admisión de {trabajo.visible}: {resultado.get('decision') or 'rechazada'}; no se copió nada."
    if trabajo.estado == "cancelada":
        return (
            f"⏹️ La admisión de {trabajo.visible} se canceló antes de terminar (Sky-Claw se cerró); si llegó a "
            "copiar, volvé a admitirlo antes de auditarlo."
        )
    return (
        f"❌ La admisión de {trabajo.visible} no se hizo: {str(resultado.get('message') or 'error sin detalle')[:300]}"
    )


class GestorDeAdmisionesNativas:
    """Una admisión a la vez, fuera del round del router, con aviso al terminar.

    ``notificar`` es la superficie de avisos del guard (``HITLGuard.notify_operator``): best-effort,
    nunca decide el resultado. Lo construye el ``AppContext``, que registra :meth:`cerrar` en su
    cleanup para que un apagado cancele el trabajo vivo (la cancelación mata el proceso del bridge).
    """

    def __init__(self, *, notificar: Callable[[str], Awaitable[None]] | None = None) -> None:
        self._notificar = notificar
        self._actual: TrabajoDeAdmision | None = None
        # Toda tarea sin terminar, no solo la actual: una admisión queda en estado terminal ANTES de que
        # termine su aviso de fin, y si entra otra en esa ventana, la anterior no puede quedar huérfana.
        self._tareas: set[asyncio.Task[None]] = set()

    @property
    def actual(self) -> TrabajoDeAdmision | None:
        return self._actual

    def en_curso(self, plugin: str | None = None) -> TrabajoDeAdmision | None:
        """El trabajo vivo (de ``plugin``, si se pasa; los nombres de plugin no distinguen mayúsculas)."""
        trabajo = self._actual
        if trabajo is None or trabajo.estado not in _ESTADOS_VIVOS:
            return None
        if plugin is not None and trabajo.plugin.casefold() != plugin.casefold():
            return None
        return trabajo

    def iniciar(self, service: XEditBridgeService, hitl: HITLGuard, plugin: str) -> dict[str, Any]:
        """Lanza la admisión y responde YA. Sin ``await`` entre el chequeo y la ocupación: no hay carrera.

        ``ValueError`` si el nombre no es un plugin válido o no se puede mostrar entero al operador.
        """
        validar_plugin(plugin)
        visible = nombre_visible(plugin)
        vivo = self.en_curso()
        if vivo is not None:
            return {
                "success": False,
                "message": f"ya hay una admisión en curso ({vivo.id}, {_nombre(vivo.plugin)}): esperá su aviso",
                "trabajo_id": vivo.id,
                "estado": vivo.estado,
            }
        perfil = sanitize_for_prompt(service.perfil_activo(), max_length=120)
        trabajo = TrabajoDeAdmision(
            id=f"admision-{next(_IDS)}", plugin=plugin, visible=visible, iniciado_en=time.time()
        )
        trabajo.tarea = asyncio.create_task(
            self._correr(trabajo, service, hitl, perfil), name=f"xedit-bridge-{trabajo.id}"
        )
        self._tareas.add(trabajo.tarea)
        trabajo.tarea.add_done_callback(self._tareas.discard)
        self._actual = trabajo
        return {
            "success": True,
            "message": "",
            "estado": trabajo.estado,
            "trabajo_id": trabajo.id,
            "plugin": _nombre(plugin),
            "instrucciones": (
                "El operador recibe por este canal el pedido de aprobación; sin su aprobación no se copia "
                "nada. Sky-Claw avisa cuando la admisión termina; después, auditar_plugin_nativo."
            ),
        }

    async def _correr(
        self, trabajo: TrabajoDeAdmision, service: XEditBridgeService, hitl: HITLGuard, perfil: str
    ) -> None:
        try:
            decision = await _pedir_aprobacion_de_admision(hitl, trabajo.visible, perfil)
            if decision is not Decision.APPROVED:
                logger.warning("Admisión de %s no aprobada: %s", trabajo.visible, decision.value)
                trabajo.estado = "rechazada"
                resultado: dict[str, Any] = {
                    "success": False,
                    "message": "Admisión rechazada por el operador: no se copió nada.",
                    "status": "denied",
                    "decision": decision.value,
                }
            else:
                trabajo.estado = "en_curso"
                resultado = await service.admitir(trabajo.plugin)
        except asyncio.CancelledError:
            await self._registrar_cancelacion(trabajo)
            raise
        except Exception as exc:  # noqa: BLE001 -- frontera del trabajo: el servicio no debería lanzar
            logger.exception("la admisión remota (%s) lanzó en vez de devolver su resultado", trabajo.id)
            resultado = {"success": False, "message": f"error inesperado de la admisión: {exc}"}
        await self._terminar(trabajo, resultado)

    async def _terminar(self, trabajo: TrabajoDeAdmision, resultado: dict[str, Any]) -> None:
        trabajo.resultado = resultado
        if trabajo.estado != "rechazada":
            trabajo.estado = "exitosa" if resultado.get("success") is True else "fallida"
        trabajo.terminado_en = time.time()
        await self._avisar(_aviso_de_fin(trabajo))

    async def _registrar_cancelacion(self, trabajo: TrabajoDeAdmision) -> None:
        """Cierra el trabajo como ``cancelado`` y avisa UNA vez (acotado: corre durante el apagado)."""
        if trabajo.estado not in _ESTADOS_VIVOS:
            return
        trabajo.estado = "cancelada"
        trabajo.terminado_en = time.time()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(self._avisar(_aviso_de_fin(trabajo)), timeout=PLAZO_DEL_AVISO_DE_CANCELACION_S)

    async def _avisar(self, mensaje: str) -> None:
        if self._notificar is None:
            logger.info("admisión remota: %s", mensaje)
            return
        try:
            await self._notificar(mensaje)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- aviso best-effort
            logger.warning("no se pudo avisar al operador del fin de la admisión", exc_info=True)

    async def cerrar(self) -> None:
        """Shutdown: cancela TODA tarea sin terminar (la admisión viva y avisos pendientes) y las espera."""
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
                logger.warning("una admisión remota terminó con error durante el cierre", exc_info=True)
        # Una task cancelada ANTES de entrar a `_correr` nunca ejecuta su handler. No-op si ya cerró.
        if self._actual is not None:
            await self._registrar_cancelacion(self._actual)


__all__ = [
    "MARGEN_LLM_S",
    "PRESUPUESTO_RESULTADO_LLM",
    "GestorDeAdmisionesNativas",
    "TrabajoDeAdmision",
    "admitir_plugin_nativo",
    "auditar_plugin_nativo",
]
