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
—Wrye Bash (``[bash, -b, "Bashed Patch, 0.esp"]`` con ``cwd=game_path``) y
BodySlide (``-o`` relativo al ``cwd``)— escribe **físicamente** relativo a su
``cwd``. Pandora usa (b): Sky-Claw le pasa una ruta explícita absoluta y
administrada bajo ``Pandora_Output``. Esta propiedad está verificada por código
y tests; no afirma un rig real de Pandora/MO2.

**Por qué es un módulo y no un comentario.** La premisa contraria ("el destino es
dependiente del entorno") estaba reescrita a mano en nueve lugares del árbol, y
en tres de ellos era el motivo declarado de que NO hubiera rollback
(``target_files=[]`` / ``snapshots=[]``, U-04). Mientras cada servicio la
reescribiera por su cuenta, corregir uno dejaba a los otros ocho — el defecto #1
del repo. Acá se enuncia una vez; ``tests/test_output_targets.py`` enumera la
familia y falla ante un servicio nuevo sin clasificar.

**Y las afirmaciones de este módulo se verifican contra el spawn, no contra otro
comentario.** El primer intento de U-01 parte 2 excluyó el ``cwd`` de las raíces
de DynDOLOD porque un comentario vecino decía que no era el del subproceso; el
``create_subprocess_exec`` decía lo contrario (review CodeRabbit #388). Con
``-o:`` esa discusión murió: DynDOLOD/TexGen reciben la raíz administrada única
y escriben solo ahí — la rama "el cwd es una raíz legítima" dejó de ser verdadera.
Cada rama de acá cita el mecanismo concreto que la sostiene.

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

#: Subdirectorio administrado que Sky-Claw pasa a Pandora como ruta de salida
#: explícita absoluta. Vive acá para que todos los consumidores compartan el
#: resolver sin ciclos de imports.
PANDORA_OUTPUT_DIR = "Pandora_Output"

#: Nombre del directorio de salida de Synthesis bajo ``mods/`` cuando no hay
#: ``overwrite`` en disco.
SYNTHESIS_MOD_NAME = "Synthesis Output"

#: Directorio administrado por Sky-Claw para la salida de BodySlide (U-04).
#: Sustituye al ``output_path`` que antes elegía el LLM como ruta física
#: literal: ese valor podía resolver bajo ``Data/meshes``, un árbol compartido
#: por miles de archivos de otros mods donde un move-aside de directorio
#: completo sería destructivo. Vive acá, no en ``system_tools``, por el mismo
#: motivo que ``PANDORA_OUTPUT_DIR``: que todos los consumidores compartan el
#: resolver sin ciclos de imports.
BODYSLIDE_OUTPUT_DIR = "BodySlide_Output"

#: Lock resource id de BodySlide. Vive acá (no en ``system_tools``) para que
#: ``rollback_reconciler`` lo consuma sin cruzar de ``local/tools`` hacia
#: ``app/agent/tools`` — mismo motivo que ``BEHAVIOR_GRAPHS_RESOURCE_ID`` vive
#: en ``pandora_service`` y no en ``system_tools.run_pandora``.
BODYSLIDE_MESHES_RESOURCE_ID = "bodyslide-meshes"

#: Raíz administrada única de Sky-Claw para la salida de DynDOLOD/TexGen. Lleva
#: el nombre de la FAMILIA, no el de una salida: la herramienta crea sus propias
#: carpetas (``DynDOLOD_Output``/``TexGen_Output``) DENTRO del valor de ``-o:`` —
#: el layout por default (la salida aparece junto al exe) lo confirma. Nombrarla
#: con el staging anidaría ``DynDOLOD_Output/DynDOLOD_Output``.
DYNDOLOD_OUTPUT_ROOT = "DynDOLOD"


def pandora_output_target(*, game: pathlib.Path | None) -> pathlib.Path | None:
    """Salida física única administrada, pasada por ``--output``.

    No admite override externo porque el mismo árbol se restaura
    transaccionalmente.
    """
    if game is None:
        return None
    return game.resolve() / PANDORA_OUTPUT_DIR


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


def bodyslide_output_root(*, game: pathlib.Path | None) -> pathlib.Path | None:
    """Raíz EXCLUSIVA de Sky-Claw para la salida de BodySlide.

    "Exclusiva" no es un adjetivo suelto: nada más que ``run_bodyslide_batch``
    crea contenido acá (a diferencia de ``mods/`` o ``game/``, que sí son
    compartidos). Esa propiedad es la que ``rollback_reconciler`` usa para
    descubrir destinos por escaneo en vez de declararlos a mano — ver
    :mod:`sky_claw.local.tools.rollback_reconciler`.
    """
    if game is None:
        return None
    return game.resolve() / BODYSLIDE_OUTPUT_DIR


def bodyslide_output_target(*, game: pathlib.Path | None, group: str) -> pathlib.Path | None:
    """Salida física administrada de BodySlide: un subárbol propio POR GRUPO.

    Un directorio único compartido entre grupos violaría la propiedad que
    :class:`~sky_claw.local.tools._dir_rollback.DirectoryRollback` exige (*"la
    herramienta regenera el target por completo"*): ``-b <group>`` sólo
    (re)escribe las mallas de ESE grupo, así que un rebuild de ``"CBBE"``
    sobre un directorio compartido descartaría lo que ya existía de ``"3BA"``
    al restaurar/descartar el backup. Un subárbol por grupo hace que cada
    corrida sea, para su propio target, exactamente la propiedad que el
    move-aside necesita.

    ``group`` llega ya validado (patrón + rechazo de ``"."``/``".."`` como
    componente completo, PR de U-04) por ``AsyncToolRegistry.execute()`` vía
    ``BodySlideBatchParams`` antes de llegar acá — pero esta función es
    pública y nada en su firma impide un llamador futuro que la invoque sin
    pasar por ese validador. Defensa en profundidad (review CodeRabbit, PR
    #430): revalida ``"."``/``".."`` como valor completo y separadores de ruta
    ANTES de construir el target, con el mismo comportamiento de error que ya
    usa esta función para ``game=None`` (``None``, no una excepción).
    """
    if group in (".", "..") or "/" in group or "\\" in group:
        return None
    root = bodyslide_output_root(game=game)
    if root is None:
        return None
    return root / group


def dyndolod_output_target(*, game: pathlib.Path | None) -> pathlib.Path | None:
    """Raíz administrada única de Sky-Claw para la salida de DynDOLOD/TexGen.

    Los binarios la reciben vía ``-o:`` (patrón (b): ruta explícita en el comando)
    y crean sus carpetas de salida —``DynDOLOD_Output``/``TexGen_Output``, las
    constantes del runner— adentro. Que el destino sea único y conocido reemplaza
    la adivinanza entre tres raíces (MO2, dir del exe, cwd) y es lo que habilita
    el post-check de artefacto (U-06) sobre un target administrado.

    El runner resuelve el staging real con un fallback acotado a esta raíz
    (``root/<StagingName>`` y, si la herramienta escribiera directo, ``root``);
    acá vive solo la raíz. ``None`` sin juego, mismo contrato que
    :func:`pandora_output_target`.
    """
    if game is None:
        return None
    return game.resolve() / DYNDOLOD_OUTPUT_ROOT
