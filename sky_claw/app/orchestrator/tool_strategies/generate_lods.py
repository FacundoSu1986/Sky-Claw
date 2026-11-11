"""Strategy for the `generate_lods` tool."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sky_claw.local.tools.dyndolod_uia_contrato import ModoDeInteraccion, modo_de_interaccion_desde
from sky_claw.local.tools.dyndolod_uia_gate import ResultadoConfirmacion
from sky_claw.local.validators.etapas_previas_lods import etapas_previas_faltantes

if TYPE_CHECKING:
    from sky_claw.local.tools.dyndolod_service import DynDOLODPipelineService

logger = logging.getLogger(__name__)

#: El canal humano al que se le pide seguir SIN las etapas previas: recibe cada etapa faltante y responde
#: con la decisión del operador. Misma forma que el del camino remoto (``lod_tools.AprobadorDeEtapasPrevias``).
AprobadorDeEtapasPrevias = Callable[[tuple[str, ...]], Awaitable[ResultadoConfirmacion]]

#: ``interaction_mode`` (#661) viaja igual que las demás claves: la superficie GUI
#: (dispatcher) y la remota (tool del agente) piden el MISMO parámetro del
#: servicio — ancla de paridad en ``tests/test_dyndolod_desatendido_remoto.py``.
_VALID_LOD_KEYS = {"preset", "run_texgen", "create_snapshot", "texgen_args", "dyndolod_args", "interaction_mode"}


class GenerateLodsStrategy:
    name = "generate_lods"

    def __init__(
        self,
        service: DynDOLODPipelineService,
        aprobar_sin_etapas_previas: AprobadorDeEtapasPrevias | None = None,
    ) -> None:
        self.service = service
        # D-3: el canal por el que el operador de la GUI confirma seguir en modo ASISTIDO sin las etapas previas.
        # ``None`` significa que no hay a quién preguntarle, y entonces la corrida asistida con etapas faltantes
        # se corta (fail-closed): el guard nunca se salta en silencio.
        self._aprobar_sin_etapas_previas = aprobar_sin_etapas_previas

    def describe_for_approval(self, payload_dict: dict[str, Any]) -> str:
        # warn=False: execute() filters (and warns) again for the same payload —
        # warning here too would duplicate the log line per invocation.
        filtered = self._filter_payload(payload_dict, warn=False)
        if not filtered:
            return "payload: <empty>"
        parts = [f"{key}={value!r}" for key, value in sorted(filtered.items())]
        return "payload: " + ", ".join(parts)

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        filtered = self._filter_payload(payload_dict)
        modo = filtered.get("interaction_mode")
        if _pide_modo_asistido(modo):
            # D-3 (decisión del owner, 2026-11-11, que revirtió la exclusión anterior de SOP §5.2): la corrida
            # ASISTIDA de la GUI también verifica las etapas 6–8 y, si faltan, necesita la confirmación del
            # operador por su canal. Un modo inválido NO pasa por acá: lo rechaza el servicio.
            bloqueo = await self._guard_de_etapas_previas_del_asistido()
            if bloqueo is not None:
                return bloqueo
        elif _pide_modo_desatendido(modo):
            # SOP §5.2 (#661, review Codex P1 en #740): el modo desatendido es un
            # camino NUEVO de LODs también por el dispatcher, así que nace con el
            # guard de etapas previas. Acá no hay canal remoto para el override del
            # operador (eso es del camino remoto): sin las etapas 6–8, corta.
            faltantes = await etapas_previas_faltantes(self.service.verificar_etapas_previas)
            if faltantes:
                mensaje = (
                    "la etapa 9 desatendida no se lanza: faltan etapas previas (SOP §5.2): "
                    + "; ".join(faltantes)
                    + ". Completalas, o corré la etapa 9 en modo asistido."
                )
                logger.warning("%s", mensaje, extra={"pipeline_stage": 9})
                return {
                    "success": False,
                    "message": mensaje,
                    "errors": [mensaje],
                    "etapas_faltantes": list(faltantes),
                    "assisted": False,
                    "interaction_mode": ModoDeInteraccion.DESATENDIDO.value,
                }
        return await self.service.execute(**filtered)

    async def _guard_de_etapas_previas_del_asistido(self) -> dict[str, Any] | None:
        """``None`` si la corrida asistida puede arrancar; si no, el resultado de error ya armado.

        Puede arrancar cuando las etapas 6–8 están completas, o cuando faltan y el operador confirma seguir igual.
        Cualquier otra respuesta (denegó, venció el plazo, no hay canal, el canal falló) corta antes del servicio.
        """
        faltantes = await etapas_previas_faltantes(self.service.verificar_etapas_previas)
        if not faltantes:
            return None
        decision = await self._pedir_confirmacion_sin_etapas_previas(faltantes)
        if decision is ResultadoConfirmacion.APROBADA:
            logger.warning(
                "etapa 9 asistida lanzada por override del operador con etapas previas faltantes (SOP §5.2): %s",
                "; ".join(faltantes),
                extra={"pipeline_stage": 9},
            )
            return None
        mensaje = (
            "la etapa 9 asistida no se lanza: faltan etapas previas (SOP §5.2): "
            + "; ".join(faltantes)
            + ". Completalas, o confirmá en la ventana de aprobación que querés seguir igual."
        )
        logger.warning("%s (confirmación: %s)", mensaje, decision.value, extra={"pipeline_stage": 9})
        return {
            "success": False,
            "message": mensaje,
            "errors": [mensaje],
            "etapas_faltantes": list(faltantes),
            "confirmacion": decision.value,
            "assisted": True,
            "interaction_mode": ModoDeInteraccion.ASISTIDO.value,
        }

    async def _pedir_confirmacion_sin_etapas_previas(self, faltantes: tuple[str, ...]) -> ResultadoConfirmacion:
        """Le pregunta al operador. Fail-closed: sin canal, o con un canal que explota, es «no disponible»."""
        aprobar = self._aprobar_sin_etapas_previas
        if aprobar is None:
            return ResultadoConfirmacion.CANAL_NO_DISPONIBLE
        try:
            return await aprobar(faltantes)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- canal humano: cualquier falla es «no disponible», nunca «sí»
            logger.warning(
                "el canal de confirmación de las etapas previas falló: la etapa 9 asistida no arranca",
                exc_info=True,
                extra={"pipeline_stage": 9},
            )
            return ResultadoConfirmacion.CANAL_NO_DISPONIBLE

    def _filter_payload(self, payload_dict: dict[str, Any], *, warn: bool = True) -> dict[str, Any]:
        filtered = {key: value for key, value in payload_dict.items() if key in _VALID_LOD_KEYS}
        unexpected = payload_dict.keys() - _VALID_LOD_KEYS
        if unexpected and warn:
            logger.warning("Dropping unexpected payload keys in %s: %s", self.name, unexpected)
        return filtered


def _pide_modo_asistido(valor: object) -> bool:
    """``True`` para un modo ASISTIDO válido o ausente (el default); un valor inválido lo rechaza el servicio."""
    if valor is None:
        return True
    try:
        return modo_de_interaccion_desde(valor) is ModoDeInteraccion.ASISTIDO
    except ValueError:
        return False


def _pide_modo_desatendido(valor: object) -> bool:
    """``True`` sólo para un modo DESATENDIDO válido; un valor inválido lo rechaza el servicio."""
    if valor is None:
        return False
    try:
        return modo_de_interaccion_desde(valor) is ModoDeInteraccion.DESATENDIDO
    except ValueError:
        return False
