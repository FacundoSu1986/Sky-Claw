"""Sensor de visibilidad de mods: ¿el tool va a ver el modlist? (U-01).

Sky-Claw corre **standalone**, no lanzado desde MO2 (invariante de deployment
confirmada por el mantenedor). Todos los tools externos se spawnean directo —
solo el juego pasa por el proxy ``ModOrganizer.exe`` (``mo2/vfs.py``) — así que
**ninguno hereda la USVFS de MO2**. Con una instancia MO2 en USVFS estándar (el
default), los tools leen el ``Data`` del juego base, sin un solo mod, y todo el
pipeline reporta verde desde el Stage 1 sobre un árbol vacío.

**Por qué hacía falta un sensor nuevo.** Ninguno de los existentes cubre esto:

* :class:`~sky_claw.local.validators.vfs_health.VfsHealthChecker` (T-13) solo
  detecta symlinks/junctions; nunca comprueba que los mods estén *visibles*.
* :class:`~sky_claw.local.validators.missing_masters.MissingMastersChecker`
  busca a través de TODOS los ``plugin_dirs`` (``Data`` + ``mods/*`` +
  ``overwrite``), así que encuentra el plugin dentro de ``mods/SomeMod/`` y lo
  reporta presente — exactamente el punto ciego.

**Criterio deliberadamente conservador.** Solo el caso INEQUÍVOCO bloquea: el
perfil activa N plugins de mods y **ninguno** es visible en el ``Data`` que el
tool va a leer. Eso distingue "la USVFS no se heredó" de una materialización
parcial (algunos mods desplegados a disco, otros no), que pasa en verde: un
falso negativo acotado es preferible a frenar un setup que hoy funciona.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass

from sky_claw.local.validators.preflight import PreflightCheck, PreflightStatus

logger = logging.getLogger(__name__)

#: Masters del juego base: viven en ``Data`` con o sin MO2, así que verlos NO
#: prueba que la virtualización esté activa. Se excluyen del universo medido.
_VANILLA_MASTERS: frozenset[str] = frozenset(
    {
        "skyrim.esm",
        "update.esm",
        "dawnguard.esm",
        "hearthfires.esm",
        "dragonborn.esm",
    }
)

#: Prefijo del contenido de Creation Club (``ccBGSSSE001-Fish.esm``…). Es
#: contenido OFICIAL y se instala en ``Data`` igual que los masters base.
#: Contarlo como mod sería el peor error posible acá: en un run standalone se
#: vería visible y el sensor mentiría VERDE, que es justo lo que U-01 ataca.
_CREATION_CLUB_PREFIX = "cc"

_REMEDIATION = (
    "El perfil MO2 activa mods que NO son visibles en el Data del juego. "
    "Sky-Claw corre standalone (fuera de MO2), así que no hereda la "
    "virtualización USVFS: los tools leerían el juego base y el resultado no "
    "reflejaría tu modlist. Desplegá los mods a disco para este perfil o "
    "ejecutá el Ritual a través del broker de MO2."
)


def _es_plugin_de_mod(nombre: str) -> bool:
    """True si *nombre* es un plugin aportado por un mod (no contenido oficial)."""
    bajo = nombre.lower()
    return bajo not in _VANILLA_MASTERS and not bajo.startswith(_CREATION_CLUB_PREFIX)


@dataclass(frozen=True, slots=True)
class VisibilityScan:
    """Qué plugins del perfil son visibles donde el tool lee.

    Attributes:
        configured: ``False`` si no hubo fuente para medir (sin ``Data``
            resoluble). El checkpoint lo reporta como "no configurado" en vez
            de afirmar un verde que nadie verificó (lección #250).
        mod_plugins: Plugins habilitados del perfil que aporta un mod — el
            universo medido (excluye masters base y Creation Club).
        visible: Subconjunto de ``mod_plugins`` presente en el ``Data`` real.
    """

    configured: bool
    mod_plugins: tuple[str, ...]
    visible: tuple[str, ...]


class VfsVisibilityChecker:
    """Compara el modlist habilitado contra lo que hay en el ``Data`` real.

    Args:
        game_data_dir: ``<juego>/Data`` — la ruta que los tools leen cuando
            corren fuera del VFS. ``None`` = sin señal (``configured=False``).
        enabled_plugins: Plugins activos del perfil MO2, tal como los resuelve
            :func:`~sky_claw.local.mo2.plugin_sources.resolve_plugin_sources`.
    """

    def __init__(
        self,
        *,
        game_data_dir: pathlib.Path | None,
        enabled_plugins: tuple[str, ...] = (),
    ) -> None:
        self._game_data_dir = game_data_dir
        self._enabled_plugins = enabled_plugins

    def check(self) -> VisibilityScan:
        """Mide la visibilidad; nunca lanza (best-effort, como todo preflight)."""
        if self._game_data_dir is None:
            return VisibilityScan(configured=False, mod_plugins=(), visible=())

        mod_plugins = tuple(p for p in self._enabled_plugins if _es_plugin_de_mod(p))
        if not mod_plugins:
            # Perfil vainilla: no hay nada que afirmar sobre la virtualización.
            return VisibilityScan(configured=True, mod_plugins=(), visible=())

        visible: list[str] = []
        for nombre in mod_plugins:
            try:
                if (self._game_data_dir / nombre).is_file():
                    visible.append(nombre)
            except OSError:  # noqa: PERF203 — ruta por ruta: una ilegible no tumba el scan
                logger.debug("No se pudo inspeccionar %s en %s", nombre, self._game_data_dir, exc_info=True)

        if not visible:
            logger.warning(
                "Visibilidad VFS: %d plugin(s) de mod habilitados y NINGUNO visible en %s",
                len(mod_plugins),
                self._game_data_dir,
            )
        return VisibilityScan(configured=True, mod_plugins=mod_plugins, visible=tuple(visible))


def visibility_preflight_check(scan: VisibilityScan) -> PreflightCheck:
    """Traduce el scan al semáforo del preflight.

    ROJO solo ante el caso inequívoco (mods habilitados, cero visibles): ahí el
    Ritual correría contra el juego base y su resultado sería basura silenciosa.
    """
    if not scan.configured:
        return PreflightCheck(
            name="vfs_visibility",
            status=PreflightStatus.GREEN,
            summary="Sensor de visibilidad de mods no configurado.",
        )
    if not scan.mod_plugins:
        return PreflightCheck(
            name="vfs_visibility",
            status=PreflightStatus.GREEN,
            summary="El perfil no habilita plugins de mods.",
        )
    if scan.visible:
        return PreflightCheck(
            name="vfs_visibility",
            status=PreflightStatus.GREEN,
            summary=f"{len(scan.visible)}/{len(scan.mod_plugins)} plugin(s) de mod visibles en Data.",
        )
    invisibles = scan.mod_plugins
    muestra = invisibles[:10]
    return PreflightCheck(
        name="vfs_visibility",
        status=PreflightStatus.RED,
        summary=(
            f"Ninguno de los {len(invisibles)} plugin(s) de mod del perfil es visible en Data: "
            "el Ritual correría sobre el juego base."
        ),
        details=(*(f"no visible: {p}" for p in muestra), _REMEDIATION),
    )


__all__ = [
    "VfsVisibilityChecker",
    "VisibilityScan",
    "visibility_preflight_check",
]
