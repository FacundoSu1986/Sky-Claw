"""Etapa 9 (TexGen + DynDOLOD) desde la superficie REMOTA: Telegram / ``/api/chat`` (#661).

**El problema que cierra.** La etapa 9 no era alcanzable desde el agente LLM: el
registro de tools no tenía ninguna de DynDOLOD, y aunque la tuviera, el round de
tools del router corta a los 120 s (``DEFAULT_TOOL_ROUND_TIMEOUT``) mientras una
corrida real dura de minutos a horas. Cancelar la tool a los 120 s mataría la
generación a mitad.

**La forma.** Un TRABAJO en segundo plano, uno a la vez:

* ``generate_lods`` lo lanza y responde enseguida (``estado="iniciado"``). Las
  aprobaciones remotas (configuración desatendida, modales) y el resultado
  llegan después por la superficie de avisos del ``HITLGuard`` —Telegram—.
* ``lods_status`` responde el estado del trabajo en curso o el último.

**Un recurso, una autoridad.** El trabajo NO construye un servicio propio:
delega en el MISMO ``DynDOLODPipelineService`` que el ``SupervisorAgent`` armó en
el composition root (lock de etapa 9, coordinación cross-process, workspace con
su lease, readiness y capacidad desatendida), publicado en el ``AppContext`` por
el bootloader de la GUI. Sin ese servicio (``--mode telegram`` no levanta el
supervisor) la tool responde ``success=false`` con la razón; no hay segundo
constructor del servicio que pudiera divergir.

**Guard de etapas previas (SOP §5 regla 2).** Todo camino nuevo de LODs tiene
que probar que Wrye Bash, Synthesis y el grass cache ya corrieron. Antes de
lanzar, el gestor le pide al servicio la evidencia
(``verificar_etapas_previas``, que lee el MISMO perfil que alimenta el ``-p:``).
Si falta algo, la etapa 9 no arranca sola: el override es una aprobación del
OPERADOR por el canal remoto (``aprobar_sin_etapas_previas``), no un parámetro de
la tool que el modelo pueda poner. Sin canal, se corta antes de lanzar.

**Política HITL de esta capa**: lock-only, sin middleware general (#217). La
aprobación que importa en modo desatendido la pide el propio pipeline con una
categoría no auto-aprobable, por el canal remoto.
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
from typing import Any, Literal

from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion
from sky_claw.local.validators.etapas_previas_lods import etapas_previas_faltantes

logger = logging.getLogger(__name__)

EstadoDeTrabajo = Literal["iniciado", "esperando_aprobacion", "en_curso", "exitoso", "fallido", "cancelado"]

#: Cota del aviso de cancelación. Corre DURANTE el apagado de la app (que le da
#: 5 s a todo el cierre del trabajo): un Telegram lento no puede retenerlo.
PLAZO_DEL_AVISO_DE_CANCELACION_S = 3.0

#: Estados de un trabajo vivo (ocupa el único lugar del gestor).
_ESTADOS_VIVOS = frozenset({"iniciado", "esperando_aprobacion", "en_curso"})

#: Override humano del guard de etapas previas: recibe las etapas faltantes.
AprobadorDeEtapasPrevias = Callable[[tuple[str, ...]], Awaitable[ResultadoConfirmacion]]

#: Lo que el operador lee cuando la etapa 9 no tiene servicio en esta sesión.
MOTIVO_SIN_SERVICIO = (
    "La etapa 9 (TexGen/DynDOLOD) no está disponible en esta sesión: necesita la app de escritorio "
    "de Sky-Claw (modo GUI) abierta en la PC, que es la que arma el pipeline con su lock y su "
    "workspace. En --mode telegram no hay supervisor de etapa 9."
)

_IDS = itertools.count(1)


@dataclass
class TrabajoDeLods:
    """Un trabajo de etapa 9 lanzado desde la superficie remota."""

    id: str
    parametros: dict[str, Any]
    iniciado_en: float
    estado: EstadoDeTrabajo = "iniciado"
    terminado_en: float | None = None
    resultado: dict[str, Any] | None = None
    etapas_faltantes: tuple[str, ...] = ()
    tarea: asyncio.Task[None] | None = field(default=None, repr=False)

    def resumen(self) -> dict[str, Any]:
        """Estado serializable (sin la task ni el dict crudo entero del servicio)."""
        datos: dict[str, Any] = {
            "trabajo_id": self.id,
            "estado": self.estado,
            "parametros": dict(self.parametros),
            "iniciado_en": self.iniciado_en,
            "duracion_segundos": round((self.terminado_en or time.time()) - self.iniciado_en, 1),
        }
        if self.etapas_faltantes:
            datos["etapas_faltantes"] = list(self.etapas_faltantes)
        if self.resultado is not None:
            datos["resultado"] = {
                clave: self.resultado.get(clave)
                for clave in (
                    "success",
                    "message",
                    "interaction_mode",
                    "unattended_error_codes",
                    "texgen_mod_path",
                    "dyndolod_mod_path",
                    "rolled_back",
                    "needs_deployment",
                    "handoff_action",
                )
                if clave in self.resultado
            }
        return datos


def resumen_para_el_operador(trabajo: TrabajoDeLods) -> str:
    """El aviso de fin de trabajo que llega por Telegram. Una línea de veredicto + detalle."""
    resultado = trabajo.resultado or {}
    duracion = round(((trabajo.terminado_en or time.time()) - trabajo.iniciado_en) / 60, 1)
    modo = resultado.get("interaction_mode") or trabajo.parametros.get("interaction_mode")
    if trabajo.estado == "exitoso":
        return (
            f"✅ Etapa 9 ({modo}) terminada en {duracion} min: LODs generados y empaquetados "
            f"(TexGen: {resultado.get('texgen_mod_path') or '-'}; DynDOLOD: {resultado.get('dyndolod_mod_path') or '-'})."
        )
    if trabajo.estado == "cancelado":
        return f"⏹️ Etapa 9 ({modo}) cancelada tras {duracion} min."
    codigos = resultado.get("unattended_error_codes") or []
    detalle = str(resultado.get("message") or "error sin detalle")
    prefijo = f"[{', '.join(codigos)}] " if codigos else ""
    accion = ""
    if resultado.get("needs_deployment"):
        accion = f" Acción pendiente: {resultado.get('handoff_action') or 'despliegue del TexGen Output'}."
    return f"❌ Etapa 9 ({modo}) falló tras {duracion} min: {prefijo}{detalle[:900]}{accion}"


class GestorDeTrabajosDeLods:
    """Un trabajo de etapa 9 a la vez, en segundo plano, con aviso al terminar.

    ``proveedor_de_servicio`` se resuelve en CADA lanzamiento (el servicio se
    publica después de construir el registro de tools). ``notificar`` es la
    superficie de avisos del guard: best-effort, nunca decide el resultado.
    ``aprobar_sin_etapas_previas`` es el override humano del guard de etapas
    previas; ``None`` significa que no hay a quién preguntar y se corta.
    """

    def __init__(
        self,
        *,
        proveedor_de_servicio: Callable[[], Any | None],
        notificar: Callable[[str], Awaitable[None]] | None = None,
        aprobar_sin_etapas_previas: AprobadorDeEtapasPrevias | None = None,
    ) -> None:
        self._proveedor = proveedor_de_servicio
        self._notificar = notificar
        self._aprobar_sin_etapas_previas = aprobar_sin_etapas_previas
        self._actual: TrabajoDeLods | None = None
        # La verificación de etapas corre en un hilo: sin este lock, dos
        # lanzamientos concurrentes pasarían los dos el "ya hay uno en curso"
        # antes de que cualquiera ocupe el lugar.
        self._lanzamiento = asyncio.Lock()

    @property
    def actual(self) -> TrabajoDeLods | None:
        return self._actual

    def en_curso(self) -> bool:
        return self._actual is not None and self._actual.estado in _ESTADOS_VIVOS

    async def iniciar(
        self,
        *,
        preset: str,
        run_texgen: bool,
        create_snapshot: bool,
        interaction_mode: str,
    ) -> dict[str, Any]:
        """Lanza el trabajo y responde YA. Nunca espera una decisión humana en el round del router."""
        async with self._lanzamiento:
            if self.en_curso() and self._actual is not None:
                return {
                    "success": False,
                    "message": f"ya hay una etapa 9 en curso (trabajo {self._actual.id}); consultá lods_status",
                    "trabajo_id": self._actual.id,
                }
            servicio = self._proveedor()
            if servicio is None:
                return {"success": False, "message": MOTIVO_SIN_SERVICIO}
            faltantes = await self._etapas_faltantes(servicio)
            if faltantes and self._aprobar_sin_etapas_previas is None:
                return {
                    "success": False,
                    "message": "la etapa 9 no se lanza: faltan etapas previas (SOP §5.2): " + "; ".join(faltantes),
                    "etapas_faltantes": list(faltantes),
                }
            parametros = {
                "preset": preset,
                "run_texgen": run_texgen,
                "create_snapshot": create_snapshot,
                "interaction_mode": interaction_mode,
            }
            trabajo = TrabajoDeLods(
                id=f"lods-{next(_IDS)}",
                parametros=parametros,
                iniciado_en=time.time(),
                estado="esperando_aprobacion" if faltantes else "iniciado",
                etapas_faltantes=faltantes,
            )
            trabajo.tarea = asyncio.create_task(self._correr(trabajo, servicio), name=f"etapa9-{trabajo.id}")
            self._actual = trabajo
        instruccion = (
            "Te llega por este canal la aprobación de la configuración; después Sky-Claw opera "
            "TexGen/DynDOLOD solo y te avisa al terminar."
            if interaction_mode == "desatendido"
            else "Modo asistido: alguien tiene que operar la ventana de TexGen/DynDOLOD en la PC."
        )
        respuesta: dict[str, Any] = {
            "success": True,
            "message": "",
            "estado": trabajo.estado,
            "trabajo_id": trabajo.id,
            "instrucciones": instruccion,
        }
        if faltantes:
            respuesta["etapas_faltantes"] = list(faltantes)
            respuesta["instrucciones"] = (
                "Faltan etapas previas: primero te llega por este canal la pregunta de si seguir igual; "
                "sin tu aprobación la etapa 9 no arranca. " + instruccion
            )
        return respuesta

    async def _etapas_faltantes(self, servicio: Any) -> tuple[str, ...]:
        """Evidencia de las etapas 6–8 del servicio. Fail-closed: no poder verificar es faltar."""
        return await etapas_previas_faltantes(servicio.verificar_etapas_previas)

    async def _correr(self, trabajo: TrabajoDeLods, servicio: Any) -> None:
        try:
            if trabajo.etapas_faltantes and not await self._operador_aprueba_seguir(trabajo):
                await self._terminar(trabajo, trabajo.resultado or {"success": False, "message": ""})
                return
            trabajo.estado = "en_curso"
            resultado = await servicio.execute(**trabajo.parametros)
        except asyncio.CancelledError:
            trabajo.estado = "cancelado"
            trabajo.terminado_en = time.time()
            # Quien lanzó la etapa 9 desde Telegram no está en la PC: sin este
            # aviso, un apagado de la app lo deja esperando un veredicto que no
            # llega. Acotado y best-effort; la cancelación se re-lanza igual.
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(
                    self._avisar(resumen_para_el_operador(trabajo)),
                    timeout=PLAZO_DEL_AVISO_DE_CANCELACION_S,
                )
            raise
        except Exception as exc:  # noqa: BLE001 -- boundary del trabajo: el servicio no debería lanzar
            logger.exception("la etapa 9 remota (%s) lanzó en vez de devolver su resultado", trabajo.id)
            resultado = {"success": False, "message": f"error inesperado del pipeline: {exc}"}
        await self._terminar(
            trabajo, resultado if isinstance(resultado, dict) else {"success": False, "message": str(resultado)}
        )

    async def _operador_aprueba_seguir(self, trabajo: TrabajoDeLods) -> bool:
        """Override humano del guard. Cualquier cosa que no sea APROBADA deja el resultado y corta."""
        aprobar = self._aprobar_sin_etapas_previas
        decision = (
            ResultadoConfirmacion.CANAL_NO_DISPONIBLE if aprobar is None else await aprobar(trabajo.etapas_faltantes)
        )
        if decision is ResultadoConfirmacion.APROBADA:
            logger.warning(
                "etapa 9 remota (%s) lanzada SIN etapas previas por aprobación del operador: %s",
                trabajo.id,
                "; ".join(trabajo.etapas_faltantes),
            )
            return True
        trabajo.resultado = {
            "success": False,
            "message": (
                f"la etapa 9 no se lanzó: faltan etapas previas (SOP §5.2) y la aprobación para seguir "
                f"igual quedó en {decision.value}: " + "; ".join(trabajo.etapas_faltantes)
            ),
        }
        return False

    async def _terminar(self, trabajo: TrabajoDeLods, resultado: dict[str, Any]) -> None:
        trabajo.resultado = resultado
        trabajo.estado = "exitoso" if resultado.get("success") is True else "fallido"
        trabajo.terminado_en = time.time()
        await self._avisar(resumen_para_el_operador(trabajo))

    async def _avisar(self, mensaje: str) -> None:
        if self._notificar is None:
            logger.info("etapa 9 remota: %s", mensaje)
            return
        try:
            await self._notificar(mensaje)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- aviso best-effort
            logger.warning("no se pudo avisar al operador del fin de la etapa 9", exc_info=True)

    def estado(self) -> dict[str, Any]:
        if self._actual is None:
            return {"success": True, "message": "", "estado": "sin_trabajos"}
        return {"success": True, "message": "", **self._actual.resumen()}

    async def cerrar(self) -> None:
        """Shutdown: cancela el trabajo vivo y espera a que el pipeline limpie."""
        trabajo = self._actual
        if trabajo is None or trabajo.tarea is None or trabajo.tarea.done():
            return
        trabajo.tarea.cancel()
        try:
            await trabajo.tarea
        except asyncio.CancelledError:
            # Se absorbe sólo la cancelación que `cerrar` le pidió al trabajo. Si
            # cancelaron a quien cierra (p. ej. `stop()` venció su plazo), es suya
            # y se propaga.
            actual = asyncio.current_task()
            if actual is not None and actual.cancelling():
                raise
        except Exception:  # noqa: BLE001 -- shutdown best-effort
            logger.warning("el trabajo de etapa 9 terminó con error durante el cierre", exc_info=True)


async def generate_lods(
    gestor: GestorDeTrabajosDeLods | None,
    *,
    preset: str,
    run_texgen: bool,
    create_snapshot: bool,
    interaction_mode: str,
) -> str:
    """Handler de la tool ``generate_lods`` del agente (JSON con ``success``/``message``)."""
    if gestor is None:
        return json.dumps({"success": False, "message": MOTIVO_SIN_SERVICIO}, ensure_ascii=False)
    return json.dumps(
        await gestor.iniciar(
            preset=preset,
            run_texgen=run_texgen,
            create_snapshot=create_snapshot,
            interaction_mode=interaction_mode,
        ),
        ensure_ascii=False,
        default=str,
    )


async def lods_status(gestor: GestorDeTrabajosDeLods | None) -> str:
    """Handler de la tool ``lods_status`` del agente."""
    if gestor is None:
        return json.dumps({"success": False, "message": MOTIVO_SIN_SERVICIO}, ensure_ascii=False)
    return json.dumps(gestor.estado(), ensure_ascii=False, default=str)
