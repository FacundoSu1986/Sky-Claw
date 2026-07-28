"""Dónde aterriza la salida de cada ritual — fuente única (U-01 parte 2).

**El invariante, enunciado como propiedad del mecanismo.** Hay exactamente dos
formas de que la salida de un tool termine en el ``overwrite`` de MO2:

**(a) Redirección USVFS.** El tool escribe en ``Data/x`` y la VFS lo desvía a
``overwrite/x``. **Exige heredar la VFS**, y solo la hereda un proceso lanzado a
través de ``ModOrganizer.exe``. Todos los runners de este paquete se spawnean
directo con :func:`sky_claw.local.tools._process.run_capture`, así que **ninguno
la hereda**: para ellos (a) es inalcanzable. El único camino del repo que sí
corre bajo la USVFS es el broker (``local/mo2/brokered_loot.py``), cuya cobertura
productiva son ``health`` y dos entry points de LOOT.

**(b) Ruta explícita.** Al tool se le pasa la ruta en el comando y escribe ahí
**físicamente**. Funciona standalone y es perfectamente válido: es lo que hace
Synthesis apuntando al ``overwrite``.

De ahí sale la regla que este módulo materializa: **el ``overwrite`` solo se
alcanza por (b), nunca por (a)**. Quien no lleva la ruta en su línea de comandos
—Wrye Bash (``[bash, -b, "Bashed Patch, 0.esp"]`` con ``cwd=game_path``), Pandora
(``--auto``), BodySlide (``-o`` relativo al ``cwd``)— escribe **físicamente**
relativo a su ``cwd``, y su destino es resoluble sin adivinar.

**Por qué es un módulo y no un comentario.** La premisa contraria ("el destino es
dependiente del entorno") estaba escrita en seis lugares, y en tres de ellos era
el motivo declarado de que NO hubiera rollback (``target_files=[]`` /
``snapshots=[]``, U-04). Mientras cada servicio la reescribiera por su cuenta,
corregir uno dejaba a los otros cinco — el defecto #1 del repo. Acá se enuncia
una vez; ``tests/test_output_targets.py`` enumera la familia y falla ante un
servicio nuevo sin clasificar.

Hermano del lado de la ENTRADA:
``LootSortingService._routes_through_physical_data`` (``loot_service.py``)
responde la misma pregunta —¿este run lee el ``Data`` físico o corre bajo la
USVFS?— para el único servicio donde la respuesta no es constante.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sky_claw.local.tools.wrye_bash_runner import BASHED_PATCH_NAME

if TYPE_CHECKING:
    import pathlib

#: Subdirectorio que Pandora crea junto a su ejecutable cuando no escribe en el
#: ``Data``. Vive acá (y no en ``pandora_service``) para que el servicio consuma
#: el resolver sin ciclo de imports.
PANDORA_OUTPUT_DIR = "Pandora_Output"

#: Nombre del directorio de salida de Synthesis bajo ``mods/`` cuando no hay
#: ``overwrite`` en disco.
SYNTHESIS_MOD_NAME = "Synthesis Output"


def bashed_patch_target(game: pathlib.Path | None) -> pathlib.Path | None:
    """Ruta física del ``Bashed Patch, 0.esp``, o ``None`` si el juego no resuelve.

    ``bash.py`` corre con ``cwd=game_path`` y **sin ruta de salida en el
    comando** (caso (a) inalcanzable, caso (b) no aplicado): el plugin cae en el
    ``Data`` del juego. Que el destino sea único y conocido es lo que habilita el
    snapshot/rollback de U-04 y el post-check de artefacto de U-06.
    """
    if game is None:
        return None
    return game / "Data" / BASHED_PATCH_NAME


def pandora_output_candidates(
    *,
    game: pathlib.Path | None,
    exe: pathlib.Path | None,
) -> list[pathlib.Path]:
    """Destinos físicos posibles de los behavior graphs de Pandora, sin duplicados.

    Pandora corre ``--auto`` con ``cwd=game_path``, sin ruta de salida: escribe
    físicamente, en el ``Data`` del juego o junto a su ejecutable (``Pandora_Output``
    incluido — un output hijo read-only con el padre escribible pasaría
    inadvertido, review #314 F2). **El ``overwrite`` no está**: llegar ahí exigiría
    la redirección USVFS que Pandora no puede heredar.

    Sigue siendo una lista y no un destino único porque cuál de las dos elige
    depende de la versión/config de Pandora, no del modo de lanzamiento — es una
    ambigüedad del tool, no del entorno.
    """
    candidates: list[pathlib.Path] = []
    if game is not None:
        candidates.append(game / "Data")
    if exe is not None:
        candidates.append(exe.parent)
        candidates.append(exe.parent / PANDORA_OUTPUT_DIR)
    return _sin_duplicados(candidates)


def synthesis_output_target(
    *,
    mo2: pathlib.Path | None,
    override: pathlib.Path | None,
) -> pathlib.Path | None:
    """Destino de Synthesis: el override del sandbox manda; si no, el de siempre.

    Synthesis **sí** recibe la ruta en su configuración (caso (b)), así que
    apuntar al ``overwrite`` es legítimo aunque el run sea standalone: la
    escritura es física, no redirigida. Se conserva el comportamiento histórico
    —``overwrite`` si existe, si no ``mods/Synthesis Output``—; lo que cambia es
    que deja de estar copiado en dos métodos que tenían que coincidir.

    ``override`` es el ``SandboxClone.overwrite_copy`` de T-27b: con él, el run
    sandboxeado escribe en el clon y no en el overwrite real.
    """
    if override is not None:
        return override
    if mo2 is None:
        return None
    overwrite = mo2 / "overwrite"
    if overwrite.exists():
        return overwrite
    return mo2 / "mods" / SYNTHESIS_MOD_NAME


def dyndolod_staging_roots(
    *,
    mo2: pathlib.Path | None,
    exe: pathlib.Path | None,
) -> list[pathlib.Path]:
    """Raíces bajo las que DynDOLOD/TexGen crean su staging crudo, sin duplicados.

    Devuelve las RAÍCES (no las rutas completas) porque los nombres de los dos
    stagings —``DynDOLOD_Output`` y ``TexGen_Output``— son constantes de clase del
    runner; que las una el llamador evita un ciclo de imports.

    **El ``cwd`` del proceso Python no está**, y esa es la corrección: no es el
    ``cwd`` del subproceso de DynDOLOD (se le fija otro), así que un staging viejo
    en el directorio de trabajo del agente no es salida de este run. Sondearlo
    hacía que la búsqueda devolviera una ruta ajena y poblada, que el guard de
    validación aceptaba como propia.
    """
    roots: list[pathlib.Path] = []
    if mo2 is not None:
        roots.append(mo2)
    if exe is not None:
        roots.append(exe.parent)
    return _sin_duplicados(roots)


def _sin_duplicados(rutas: list[pathlib.Path]) -> list[pathlib.Path]:
    """Preserva el orden y descarta repetidos (el exe puede vivir bajo la raíz MO2)."""
    vistas: set[pathlib.Path] = set()
    return [p for p in rutas if not (p in vistas or vistas.add(p))]
