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

**Política HITL de esta capa**: lock-only, sin middleware general (#217). La
aprobación que importa en modo desatendido la pide el propio pipeline con una
categoría no auto-aprobable, por el canal remoto.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

EstadoDeTrabajo = Literal["iniciado", "en_curso", "exitoso", "fallido", "cancelado"]

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
    """

    def __init__(
        self,
        *,
        proveedor_de_servicio: Callable[[], Any | None],
        notificar: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._proveedor = proveedor_de_servicio
        self._notificar = notificar
        self._actual: TrabajoDeLods | None = None

    @property
    def actual(self) -> TrabajoDeLods | None:
        return self._actual

    def en_curso(self) -> bool:
        return self._actual is not None and self._actual.estado in {"iniciado", "en_curso"}

    def iniciar(
        self,
        *,
        preset: str,
        run_texgen: bool,
        create_snapshot: bool,
        interaction_mode: str,
    ) -> dict[str, Any]:
        """Lanza el trabajo y responde YA. Nunca bloquea el round del router."""
        if self.en_curso() and self._actual is not None:
            return {
                "success": False,
                "message": f"ya hay una etapa 9 en curso (trabajo {self._actual.id}); consultá lods_status",
                "trabajo_id": self._actual.id,
            }
        servicio = self._proveedor()
        if servicio is None:
            return {"success": False, "message": MOTIVO_SIN_SERVICIO}
        parametros = {
            "preset": preset,
            "run_texgen": run_texgen,
            "create_snapshot": create_snapshot,
            "interaction_mode": interaction_mode,
        }
        trabajo = TrabajoDeLods(id=f"lods-{next(_IDS)}", parametros=parametros, iniciado_en=time.time())
        trabajo.tarea = asyncio.create_task(self._correr(trabajo, servicio), name=f"etapa9-{trabajo.id}")
        self._actual = trabajo
        instruccion = (
            "Te llega por este canal la aprobación de la configuración; después Sky-Claw opera "
            "TexGen/DynDOLOD solo y te avisa al terminar."
            if interaction_mode == "desatendido"
            else "Modo asistido: alguien tiene que operar la ventana de TexGen/DynDOLOD en la PC."
        )
        return {
            "success": True,
            "message": "",
            "estado": "iniciado",
            "trabajo_id": trabajo.id,
            "instrucciones": instruccion,
        }

    async def _correr(self, trabajo: TrabajoDeLods, servicio: Any) -> None:
        trabajo.estado = "en_curso"
        try:
            resultado = await servicio.execute(**trabajo.parametros)
        except asyncio.CancelledError:
            trabajo.estado = "cancelado"
            trabajo.terminado_en = time.time()
            raise
        except Exception as exc:  # noqa: BLE001 -- boundary del trabajo: el servicio no debería lanzar
            logger.exception("la etapa 9 remota (%s) lanzó en vez de devolver su resultado", trabajo.id)
            resultado = {"success": False, "message": f"error inesperado del pipeline: {exc}"}
        trabajo.resultado = resultado if isinstance(resultado, dict) else {"success": False, "message": str(resultado)}
        trabajo.estado = "exitoso" if trabajo.resultado.get("success") is True else "fallido"
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
            pass
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
        gestor.iniciar(
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
