"""Estrategia de la tool ``evaluar_parallax_elegibilidad`` (NP-A0k): ¿alguna malla del perfil haría que el plugin use la salida del generador?

SOLO LECTURA: no escribe nada ni lleva ``HitlGateMiddleware`` (igual que ``diagnosticar_parallax_perfil``, ``scan_asset_conflicts`` y
``preview_chain``). Es la superficie de la GUI de un recurso que el agente LLM también alcanza (``AsyncToolRegistry``): las dos llaman a
``elegibilidad_como_resultado`` —que es quien contesta— y las raíces de la instancia salen del mismo resolver
(``PathResolutionService.raices_de_instancia_mo2``), así que no pueden contestar distinto.

El perfil es el de la SESIÓN (``default_profile_getter``) y no se elige por payload, como en el agente. El único campo del payload es
``mod`` (la carpeta del mod cuyas texturas se evaluarían); tampoco se elige el plazo: lo fija la función compartida, igual para las dos
superficies. El resolver de raíces y la lectura de mallas hacen E/S sincrónica, así que corren fuera del event loop
(``asyncio.to_thread``). El resolver se consulta en CADA llamada (como el resto de las tools de la GUI); el agente usa las raíces que
``AppContext`` fijó al arrancar: si la instancia de MO2 se reconfigura con la app abierta, las dos superficies se reconcilian al reiniciar.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from sky_claw.local.native_parallax.elegibilidad import ALCANCE_DE_LA_ELEGIBILIDAD, elegibilidad_como_resultado

if TYPE_CHECKING:
    from sky_claw.app.core.path_resolver import RaicesDeInstanciaMO2

_MAXIMO_DEL_NOMBRE_DEL_MOD = 256


def _rechazo(motivo: str) -> dict[str, Any]:
    return {"success": False, "message": motivo, "alcance": ALCANCE_DE_LA_ELEGIBILIDAD}


class EvaluarElegibilidadParallaxStrategy:
    name = "evaluar_parallax_elegibilidad"

    def __init__(
        self,
        raices_provider: Callable[[], RaicesDeInstanciaMO2],
        default_profile_getter: Callable[[], str],
    ) -> None:
        self._raices_provider = raices_provider
        self._default_profile_getter = default_profile_getter

    async def execute(self, payload_dict: dict[str, Any]) -> dict[str, Any]:
        sobrantes = sorted(set(payload_dict) - {"mod"})
        if sobrantes:
            return _rechazo(
                f"El payload solo acepta 'mod' (el perfil es el de la sesión); sobran: {', '.join(sobrantes)}."
            )
        mod = payload_dict.get("mod")
        if not isinstance(mod, str) or not mod.strip() or len(mod) > _MAXIMO_DEL_NOMBRE_DEL_MOD:
            return _rechazo(
                "Falta 'mod': el nombre de la carpeta del mod (texto no vacío) cuyas texturas se evaluarían."
            )
        try:
            # El resolver hace E/S síncrona (is_dir, ModOrganizer.ini, instancias globales): también fuera del loop.
            raices = await asyncio.to_thread(self._raices_provider)
        except RuntimeError as exc:
            return _rechazo(str(exc))
        return await asyncio.to_thread(
            elegibilidad_como_resultado,
            mods_dir=raices.mods_dir,
            data_root=raices.data_root,
            perfil=self._default_profile_getter(),
            mod=mod,
        )
