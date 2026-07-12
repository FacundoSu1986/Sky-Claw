"""Staging idempotente de scripts Pascal bundleados hacia xEdit (PR-2 grass cache).

``XEditRunner.run_script`` pasa ``-script:<nombre>`` y xEdit lo resuelve contra
SU carpeta ``Edit Scripts/`` — pero nada copiaba ahí los ``.pas`` bundleados en
``sky_claw/local/xedit/scripts/``: ``list_all_conflicts.pas`` solo funcionaba
si el usuario lo copiaba a mano. :func:`stage_scripts` cierra ese gap.

Idempotente por byte-compare (no mtime/size): copiar solo si falta o difiere,
así el mtime del destino no se toca en el caso común y una versión vieja
desplegada se actualiza sola al bumpear el bundle del repo.

Fail-closed en dos bordes:
- nombre fuera del bundle (typo/traversal) → error antes de tocar disco;
- dir de xEdit inexistente → ``FileNotFoundError`` (crear ``Edit Scripts/`` es
  legítimo; materializar una instalación fantasma de xEdit, no).
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: Directorio de los scripts Pascal bundleados con el paquete (viajan en el
#: wheel vía force-include y en el exe vía datas del spec — test_packaging.py).
BUNDLED_SCRIPTS_DIR = pathlib.Path(__file__).parent / "scripts"

Action = Literal["copied", "replaced", "unchanged"]


@dataclass(frozen=True, slots=True)
class StagedScript:
    """Resultado del staging de UN script.

    Attributes:
        name: Nombre del script (``foo.pas``).
        action: ``copied`` (no existía), ``replaced`` (difería) o
            ``unchanged`` (bytes idénticos — no se tocó el destino).
        destination: Ruta final en ``Edit Scripts/``.
    """

    name: str
    action: Action
    destination: pathlib.Path


def stage_scripts(
    edit_scripts_dir: pathlib.Path,
    script_names: Sequence[str],
) -> list[StagedScript]:
    """Copia los scripts bundleados a *edit_scripts_dir* (idempotente).

    Args:
        edit_scripts_dir: La carpeta ``Edit Scripts`` de la instalación de
            xEdit. Se crea si falta, pero su parent (el dir de xEdit) debe
            existir.
        script_names: Nombres de scripts del bundle (``foo.pas``).

    Returns:
        Un :class:`StagedScript` por nombre, en el mismo orden.

    Raises:
        ValueError: Si un nombre escapa del directorio bundleado (traversal).
        FileNotFoundError: Si el script no existe en el bundle (bug de
            packaging/typo) o el dir de xEdit no existe.
    """
    bundle_root = BUNDLED_SCRIPTS_DIR.resolve()

    # Validar TODOS los nombres antes de escribir nada: un lote con un typo no
    # deja el destino a medio stagear.
    sources: list[pathlib.Path] = []
    for name in script_names:
        source = (bundle_root / name).resolve()
        if not source.is_relative_to(bundle_root):
            raise ValueError(f"El script {name!r} escapa del bundle de scripts ({bundle_root}).")
        if not source.is_file():
            raise FileNotFoundError(
                f"Script {name!r} inexistente en el bundle ({bundle_root}): ¿typo o bug de packaging?"
            )
        sources.append(source)

    parent = edit_scripts_dir.parent
    if not parent.is_dir():
        raise FileNotFoundError(
            f"El directorio de xEdit no existe ({parent}): no se materializa una instalación fantasma."
        )
    edit_scripts_dir.mkdir(exist_ok=True)

    staged: list[StagedScript] = []
    for name, source in zip(script_names, sources, strict=True):
        destination = edit_scripts_dir / name
        data = source.read_bytes()
        if destination.exists():
            if destination.read_bytes() == data:
                staged.append(StagedScript(name=name, action="unchanged", destination=destination))
                continue
            action: Action = "replaced"
        else:
            action = "copied"
        destination.write_bytes(data)
        logger.info("Script %s stageado en %s (%s)", name, destination, action)
        staged.append(StagedScript(name=name, action=action, destination=destination))
    return staged


__all__ = ["BUNDLED_SCRIPTS_DIR", "Action", "StagedScript", "stage_scripts"]
