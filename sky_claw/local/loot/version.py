"""Detección de versión de LOOT y advisory de symlinks (T-14).

libloot <0.29 resuelve la ruta real de los archivos: si la ruta del juego es
un symlink, la resolución "sale" del VFS de MO2 y LOOT queda ciego ante los
mods virtualizados (informe mmodding §3; fix en LOOT 0.29.0, cuyo libloot ya
no resuelve symlinks). El preflight (T-15) combina esto con
:class:`~sky_claw.local.validators.vfs_health.VfsHealthChecker`.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import re

from sky_claw.local.loot.binary_version import LootBinaryVersionError, read_loot_binary_version

logger = logging.getLogger(__name__)

#: Primera versión cuyo libloot no resuelve symlinks (permanece en el VFS).
LOOT_MIN_SYMLINK_SAFE: tuple[int, int, int] = (0, 29, 0)

#: Se conserva por compatibilidad de firma (``timeout=``): la detección ya no
#: lanza ningún proceso (M1).
_VERSION_TIMEOUT_SECONDS = 15

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_loot_version(output: str) -> tuple[int, int, int] | None:
    """Extrae la primera versión ``x.y.z`` del output de ``loot --version``.

    Tolera prefijos/sufijos ("LOOT v0.28.0", "0.29.1+hash build"). Devuelve
    None si no hay ninguna versión reconocible.
    """
    match = _VERSION_RE.search(output)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch))


def symlink_advisory(version: tuple[int, int, int] | None) -> str | None:
    """Advertencia de symlinks para *version*, o None si es segura.

    Una versión desconocida también advierte: asumir que está todo bien es la
    falsa red de seguridad que el preflight existe para evitar.
    """
    if version is not None and version >= LOOT_MIN_SYMLINK_SAFE:
        return None

    if version is None:
        detalle = "No se pudo detectar la versión de LOOT."
    else:
        detalle = f"LOOT {'.'.join(map(str, version))} detectado."
    return (
        f"{detalle} Las versiones anteriores a 0.29.0 resuelven symlinks y "
        "se salen del VFS de MO2 (LOOT queda ciego ante los mods). "
        "Actualizá a LOOT 0.29.0+ o eliminá los symlinks de la ruta del juego."
    )


async def detect_loot_version(
    loot_exe: pathlib.Path,
    *,
    timeout: float = _VERSION_TIMEOUT_SECONDS,
) -> tuple[int, int, int] | None:
    """Versión de LOOT leída del ``VERSIONINFO`` del binario; ``None`` si no se puede.

    M1: ya NO corre ``LOOT.exe --version``. Ese arranque toma el mutex global
    ``LOOT.Shell.Instance`` antes de parsear argumentos (loot/loot 0.29.1
    ``src/gui/qt/main.cpp``): con el GUI del operador abierto sale 0 sin
    imprimir (versión "desconocida") y, si queda colgado, bloquea al sort real.
    La lectura del PE es la misma que atestigua ``lastVersion`` antes de cada
    sort (:func:`~sky_claw.local.loot.binary_version.read_loot_binary_version`),
    así que el preflight, el manifiesto y los settings ven la MISMA versión.

    No propaga: la detección es informativa para el preflight — un binario
    ausente o sin versión atestiguable se reporta como versión desconocida (que
    también advierte). ``timeout`` se acepta por compatibilidad y se ignora.
    """
    del timeout
    # Misma normalización que el lanzador anterior (``str(loot_exe)`` en el argv):
    # identidad para un ``Path`` y nunca un TypeError para un valor no-path.
    ruta = pathlib.Path(str(loot_exe))
    try:
        texto = await asyncio.to_thread(read_loot_binary_version, ruta)
    except LootBinaryVersionError as exc:
        logger.warning("No se pudo detectar la versión de LOOT (%s): %s", loot_exe, exc)
        return None
    return parse_loot_version(texto)
