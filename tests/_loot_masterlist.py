"""Siembra masterlist + prelude provistos (PR-B) en un data root de LOOT de test.

Con ``updateMasterlist = false`` el runner exige ambos archivos antes del spawn
(:func:`~sky_claw.local.loot.masterlist_provision.verify_masterlist_provisioned`)
y el servicio sólo reutiliza una copia vigente de la MISMA fuente (marca
``.skyclaw-source``), así que la siembra escribe ambas cosas, como el daemon.
"""

from __future__ import annotations

import pathlib

from sky_claw.local.loot.masterlist_provision import masterlist_path, masterlist_url, prelude_path, prelude_url

MASTERLIST_DE_TEST = b"bash_tags: []\nplugins: []\n"
PRELUDE_DE_TEST = b"common: []\n"


def sembrar_masterlist(
    loot_data_path: pathlib.Path,
    game_folder: str = "Skyrim Special Edition",
    loot_version: str = "0.29.1",
) -> pathlib.Path:
    """Escribe masterlist y prelude no vacíos con su fuente; devuelve el data root."""
    for ruta, contenido, url in (
        (masterlist_path(loot_data_path, game_folder), MASTERLIST_DE_TEST, masterlist_url(game_folder, loot_version)),
        (prelude_path(loot_data_path), PRELUDE_DE_TEST, prelude_url(loot_version)),
    ):
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(contenido)
        ruta.with_name(ruta.name + ".skyclaw-source").write_text(url, encoding="utf-8")
    return loot_data_path
