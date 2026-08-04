"""Siembra del ``Config.xml`` de BodySlide — precondición de una corrida headless.

``BodySlideApp::OnInit`` abre dos ``wxMessageBox`` MODALES antes de construir
nada, y **ninguno de los dos mira la línea de comandos**:

- línea 243 — ``GetOutputDataPath()`` no legible/escribible. Esa función
  (líneas 614-616) devuelve ``Config["OutputDataPath"]`` o, si está vacío,
  ``Config["GameDataPath"]``: el ``-t`` que le pasamos NO participa. Con Skyrim
  instalado bajo ``Program Files`` y el daemon sin elevar, el modal salta en
  todas las corridas.
- línea 2798 — no encuentra la clave de registro del juego y ``GameDataPath``
  está vacío.

Un modal en un proceso headless no se puede cerrar: ``run_capture`` espera a
``communicate()`` hasta agotar el timeout y recién ahí mata el árbol —
posiblemente a mitad de escritura. ``CREATE_NO_WINDOW`` no cubre esto: suprime
consolas, no ventanas GUI.

**Merge, nunca sobrescritura.** El ``Config.xml`` es del usuario: trae listas
negras de BSA por juego, luces, límites de sliders y su ``ProjectPath``. Este
módulo toca exactamente las claves que necesita y deja el resto intacto.

``ProjectPath`` queda deliberadamente FUERA del conjunto sembrado: es la
respuesta a "dónde están los ``SliderSets``", que bajo MO2 sólo la puede dar la
USVFS (``ProjectUtil::GetProjectPath`` devuelve UN directorio, nunca la unión de
los N mods). Decidirla acá sería fijar en config una respuesta que corresponde a
la capa de VFS.
"""

from __future__ import annotations

import logging
import pathlib
import xml.etree.ElementTree as ET

logger = logging.getLogger("SkyClaw.BodySlideConfig")

#: Nombre del archivo que ``BodySlideApp::OnInit`` (línea 142) carga desde el
#: directorio del ejecutable.
NOMBRE_DEL_CONFIG = "Config.xml"


def sembrar_config_de_bodyslide(
    *,
    exe_dir: pathlib.Path,
    game_path: pathlib.Path,
    output_root: pathlib.Path,
) -> bool:
    """Aplica las claves que evitan los modales de arranque. Devuelve si se aplicó.

    Best-effort por diseño: ante un XML corrupto o un FS de sólo lectura devuelve
    ``False`` y loguea, sin lanzar. La siembra mejora la robustez de la corrida;
    no es una precondición dura del build, y hacerla fatal convertiría un
    ``Config.xml`` roto del usuario en un fallo del ritual con una causa que no
    tiene nada que ver.
    """
    config = exe_dir / NOMBRE_DEL_CONFIG
    valores = {
        # Sin esto BodySlide resuelve el Data por clave de registro (líneas
        # 2782-2802) y, si falla, abre el modal de la 2798.
        "GameDataPath": str((game_path / "Data").resolve()),
        # Lo que apaga el chequeo de escritura de la línea 243: apunta la ruta que
        # BodySlide considera "su salida" al subárbol administrado, en vez de al
        # Data del juego que puede no ser escribible.
        "OutputDataPath": str(output_root.resolve()),
        # Apaga el "Continue saving files to the working directory?" de
        # BuildListBodies. Hoy ese camino ya no se alcanza porque siempre pasamos
        # ``-t`` (``datapath`` no queda vacío), pero dejarlo en ``true`` mantiene
        # viva una ruta modal para cualquier invocación futura sin ``-t``.
        "WarnMissingGamePath": "false",
    }

    try:
        raiz = _cargar_o_crear(config)
    except ET.ParseError as exc:
        logger.warning(
            "Config.xml de BodySlide ilegible en '%s' (%s): se deja intacto y se sigue sin sembrar.",
            config,
            exc,
        )
        return False

    for clave, valor in valores.items():
        # ``find`` toma el PRIMER nodo, que es el mismo que lee
        # ``ConfigurationManager``: reusarlo (en vez de append) es lo que hace a
        # esta función idempotente y evita que un duplicado con el valor viejo
        # gane en silencio.
        nodo = raiz.find(clave)
        if nodo is None:
            nodo = ET.SubElement(raiz, clave)
        nodo.text = valor

    try:
        ET.ElementTree(raiz).write(config, encoding="utf-8", xml_declaration=False)
    except OSError as exc:
        logger.warning("No se pudo escribir '%s' (%s): se sigue sin sembrar.", config, exc)
        return False
    return True


def _cargar_o_crear(config: pathlib.Path) -> ET.Element:
    """Devuelve la raíz del ``Config.xml`` existente, o una nueva si no hay.

    Un ``Config.xml`` ausente es el peor caso, no el trivial: es exactamente el
    estado en que ``GameDataPath`` queda vacío y BodySlide cae en la resolución
    por registro.
    """
    if config.is_file():
        return ET.parse(config).getroot()
    return ET.Element("Config")
