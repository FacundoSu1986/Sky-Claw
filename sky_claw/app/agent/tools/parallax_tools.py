"""Tools del agente LLM para el generador nativo de parallax: el diagnóstico del perfil de MO2 (NP-A0h) y la elegibilidad (NP-A0k).

SOLO LECTURA, sin HITL (política base lock-only del agente, #217): no escriben nada. Cada una es el hermano de una estrategia del
dispatcher de la GUI (``DiagnosticarPerfilParallaxStrategy`` y ``EvaluarElegibilidadParallaxStrategy``): las dos superficies contestan
con la misma función (``diagnostico_como_resultado`` y ``elegibilidad_como_resultado``), la raíz de la instancia sale del
``MO2Controller`` que arma ``AppContext`` con las mismas raíces que pide la GUI (``PathResolutionService.raices_de_instancia_mo2``) y el
perfil es el de la sesión: el LLM solo manda ``mod``.

Los nombres de mods y las rutas de texturas son texto que eligió otro (un archivo descargado): llegan al contexto del modelo, así que se
sanean con ``sanitize_for_prompt`` (T2-04), igual que los resultados de ``search_mod``/``run_xedit_script``.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any

from sky_claw.app.security.sanitize import sanitize_for_prompt
from sky_claw.local.native_parallax.diagnostico_perfil import ALCANCE_DEL_DIAGNOSTICO, diagnostico_como_resultado
from sky_claw.local.native_parallax.elegibilidad import ALCANCE_DE_LA_ELEGIBILIDAD, elegibilidad_como_resultado

#: Tope por texto: una ruta de textura cabe de sobra; el resto es ruido que gasta contexto.
_MAXIMO_POR_TEXTO = 256


def _sanear(valor: Any) -> Any:
    """Sanea recursivamente los textos de un resultado (los números, booleanos y la leyenda de alcance no se tocan)."""
    if isinstance(valor, str):
        return sanitize_for_prompt(valor, max_length=_MAXIMO_POR_TEXTO)
    if isinstance(valor, list):
        return [_sanear(item) for item in valor]
    if isinstance(valor, dict):
        return {k: (v if k == "alcance" else _sanear(v)) for k, v in valor.items()}
    return valor


async def diagnosticar_parallax_perfil(mo2: Any, mod: str, *, profile: str) -> str:
    """Diagnostica qué gana, en el perfil de la sesión, la entrada y la salida del generador para *mod*; devuelve JSON saneado."""
    mods_dir: pathlib.Path = mo2.mods_dir
    data_root: pathlib.Path = mo2.data_root
    resultado = await asyncio.to_thread(
        diagnostico_como_resultado, mods_dir=mods_dir, data_root=data_root, perfil=profile, mod=mod
    )
    return json.dumps(_sanear(resultado), ensure_ascii=False)


async def evaluar_parallax_elegibilidad(mo2: Any, mod: str, *, profile: str) -> str:
    """Evalúa si alguna malla del perfil de la sesión haría usar a Auto Parallax la salida del generador para *mod*; devuelve JSON saneado.

    Las rutas de mallas y de texturas salen del disco (y de dentro de un NIF descargado): como en el diagnóstico, todo texto llega
    saneado al contexto del modelo.
    """
    mods_dir: pathlib.Path = mo2.mods_dir
    data_root: pathlib.Path = mo2.data_root
    resultado = await asyncio.to_thread(
        elegibilidad_como_resultado, mods_dir=mods_dir, data_root=data_root, perfil=profile, mod=mod
    )
    return json.dumps(_sanear(resultado), ensure_ascii=False)


__all__ = [
    "ALCANCE_DEL_DIAGNOSTICO",
    "ALCANCE_DE_LA_ELEGIBILIDAD",
    "diagnosticar_parallax_perfil",
    "evaluar_parallax_elegibilidad",
]
