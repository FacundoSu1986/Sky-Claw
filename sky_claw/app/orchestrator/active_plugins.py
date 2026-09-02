"""Seam puro para parsear el load order activo de plugins.

``parse_active_plugins`` se mantiene acá porque tiene más de un consumidor
(``PluginLimitGuard`` y ``RecordConflictScanner``) con políticas de lectura
**distintas** que no convierto en un único reader: este módulo es solo la
función pura de parsing; las políticas de precedencia y fallback entre
``plugins.txt``/``loadorder.txt`` viven en cada consumidor.
"""

from __future__ import annotations

from typing import Literal

PluginListSource = Literal["loadorder", "plugins_txt"]


def parse_active_plugins(load_order_text: str, *, source: PluginListSource = "loadorder") -> list[str]:
    """Extrae los plugins del load order (``loadorder.txt`` / ``plugins.txt``).

    Seam puro (testeable sin supervisor). Formato de esos archivos de MO2/Skyrim
    SE: un plugin por línea, en orden de carga; se ignoran vacíos y comentarios
    (``#``). ``loadorder.txt`` no marca habilitados: sus plugins válidos se
    conservan tal cual. ``plugins.txt`` sí usa ``*`` como marca de habilitado,
    por lo que las líneas sin ``*`` se descartan. NO confundir con
    ``modlist.txt``, que lista *mods* con prefijos ``+/-`` (review Copilot
    #226). Se conservan solo ``.esp/.esm/.esl`` — ``.esl`` incluido porque
    xEdit también reporta conflictos entre plugins ligeros.
    """
    plugins: list[str] = []
    for raw in load_order_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if source == "plugins_txt":
            if not line.startswith("*"):
                continue
            name = line[1:].strip()
        else:
            name = line
        if name.lower().endswith((".esp", ".esm", ".esl")):
            plugins.append(name)
    return plugins
