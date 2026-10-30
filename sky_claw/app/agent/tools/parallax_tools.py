"""Tools del agente LLM para el generador nativo de parallax: el diagnóstico del perfil de MO2 (NP-A0h) y la elegibilidad (NP-A0k).

SOLO LECTURA, sin HITL (política base lock-only del agente, #217): no escriben nada. Cada una es el hermano de una estrategia del
dispatcher de la GUI (``DiagnosticarPerfilParallaxStrategy`` y ``EvaluarElegibilidadParallaxStrategy``): las dos superficies contestan
con la misma función (``diagnostico_como_resultado`` y ``elegibilidad_como_resultado``), la raíz de la instancia sale del
``MO2Controller`` que arma ``AppContext`` con las mismas raíces que pide la GUI (``PathResolutionService.raices_de_instancia_mo2``) y el
perfil es el de la sesión: el LLM solo manda ``mod``.

Los nombres de mods y las rutas de texturas son texto que eligió otro (un archivo descargado): llegan al contexto del modelo, así que se
sanean con ``sanitize_for_prompt`` (T2-04), igual que los resultados de ``search_mod``/``run_xedit_script``.

El router recorta a mano, ANTES de dárselo al modelo, todo resultado de tool que pase de 4000 caracteres, y lo hace por el final: lo que
vaya al final de la respuesta es lo que se pierde. Las dos tools tienen una lista de detalle de hasta 50 entradas y una leyenda de alcance
(que dice que esto no prueba lo que se ve en el juego): si esa leyenda fuera al final, el modelo recibiría veredictos sin sus salvedades.
Por eso la respuesta se ajusta ACÁ, por debajo del recorte del router: el resumen y la leyenda van primero, el detalle va último y se le
sacan entradas de atrás (lo menos accionable, porque el resultado ya viene ordenado) hasta que cabe, marcando ``truncado``.
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

#: Caracteres que puede ocupar la respuesta al modelo. Por debajo del recorte a mano de ``LLMRouter`` (``len(resultado) > 4000``): un test
#: lee ese literal del router y ata los dos, así que subir el del router sin tocar esto (o al revés) rompe el ancla.
PRESUPUESTO_DE_CARACTERES_DE_LA_RESPUESTA = 3_800


def _sanear(valor: Any) -> Any:
    """Sanea recursivamente los textos de un resultado (los números, booleanos y la leyenda de alcance no se tocan)."""
    if isinstance(valor, str):
        return sanitize_for_prompt(valor, max_length=_MAXIMO_POR_TEXTO)
    if isinstance(valor, list):
        return [_sanear(item) for item in valor]
    if isinstance(valor, dict):
        return {k: (v if k == "alcance" else _sanear(v)) for k, v in valor.items()}
    return valor


def _para_el_modelo(resultado: dict[str, Any], clave_de_detalle: str) -> str:
    """Sanea *resultado* y lo serializa en a lo sumo ``PRESUPUESTO_DE_CARACTERES_DE_LA_RESPUESTA`` caracteres, con el detalle al final.

    El resumen y la leyenda de alcance van primero (orden de las claves del JSON) y la lista de ``clave_de_detalle`` va última: se le sacan
    entradas del final hasta que la respuesta cabe, y si se sacó alguna se marca ``truncado`` (el total real viaja en ``n_*``). Un resultado
    de fallo (sin lista de detalle) se serializa tal cual.
    """
    sano = _sanear(resultado)
    detalle = sano.pop(clave_de_detalle, None)
    if not isinstance(detalle, list):
        if detalle is not None:  # no es la lista esperada: se devuelve completo y en su sitio, sin tocar
            sano[clave_de_detalle] = detalle
        return json.dumps(sano, ensure_ascii=False)
    visibles = list(detalle)
    while True:
        candidato = {
            **sano,
            "truncado": bool(sano.get("truncado")) or len(visibles) < len(detalle),
            clave_de_detalle: visibles,
        }
        texto = json.dumps(candidato, ensure_ascii=False)
        if len(texto) <= PRESUPUESTO_DE_CARACTERES_DE_LA_RESPUESTA or not visibles:
            return texto
        visibles.pop()


async def diagnosticar_parallax_perfil(mo2: Any, mod: str, *, profile: str) -> str:
    """Diagnostica qué gana, en el perfil de la sesión, la entrada y la salida del generador para *mod*; devuelve JSON saneado."""
    mods_dir: pathlib.Path = mo2.mods_dir
    data_root: pathlib.Path = mo2.data_root
    resultado = await asyncio.to_thread(
        diagnostico_como_resultado, mods_dir=mods_dir, data_root=data_root, perfil=profile, mod=mod
    )
    return _para_el_modelo(resultado, "hallazgos")


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
    return _para_el_modelo(resultado, "texturas")


__all__ = [
    "ALCANCE_DEL_DIAGNOSTICO",
    "ALCANCE_DE_LA_ELEGIBILIDAD",
    "PRESUPUESTO_DE_CARACTERES_DE_LA_RESPUESTA",
    "diagnosticar_parallax_perfil",
    "evaluar_parallax_elegibilidad",
]
