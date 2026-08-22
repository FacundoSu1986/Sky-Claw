"""Reconciliación de arranque de los backups que sobreviven a una muerte dura (U-08).

Los locks distribuidos se auto-curan por TTL; las promesas de filesystem no. Una
muerte dura —SIGKILL, OOM, corte de luz— entre el move-aside y su restauración
evade todo ``finally``, ``__aexit__`` y ``try/except`` in-process, y deja residuo
durable en disco. La mitad 1 de U-08 (#378) cubrió la cancelación cooperativa en
``clone()``; esta es la red para lo que ningún código en vuelo puede atrapar,
porque no queda código en vuelo.

**El invariante, enunciado como propiedad del mecanismo:** *un backup solo se
descarta cuando el filesystem prueba que ya no es la única copia de algo.* De ahí
salen las tres acciones, y ninguna es "borrar porque sobró":

- **Restaurar** — el estado previo es lo único que hay, y devolverlo es lo que el
  ``__aexit__`` interrumpido habría hecho.
- **Preservar + avisar** — hay dos estados y el filesystem no dice cuál vale.
  Elegir por el operador destruye trabajo; el reconciliador informa y se corre.
- **Descartar** — hay prueba de que el residuo es copia redundante.

**Por qué no alcanza "GC de backups huérfanos" a secas** (el piso que plantea
U-08): un ``<dir>.rollback-<nonce>`` de DynDOLOD es la **única** copia de la
generación de LODs anterior, varios GB y horas de cómputo. Un reconciliador que
lo borre por estar "huérfano" es un destructor de datos con nombre amable. El
marcador durable que decide en cada familia ya está en disco — este módulo lo lee
en vez de inventar uno nuevo.

Hermano de la ENTRADA de este mismo hook:
:func:`~sky_claw.local.tools.grass_cache_service.reconcile_orphan_precache_flag`
(U-03), del que se replica el guard de concurrencia: se adquiere el lock del
ritual que produce el residuo, y si otra instancia lo tiene, no se toca nada.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import pathlib
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sky_claw.app.db.locks import LockAcquisitionError
from sky_claw.app.security.links import is_link, path_present, rmtree_link_aware
from sky_claw.local.tools.dyndolod_runner import DynDOLODRunner
from sky_claw.local.tools.output_targets import (
    BODYSLIDE_MESHES_RESOURCE_ID,
    bodyslide_output_root,
    dyndolod_output_target,
    pandora_output_target,
)
from sky_claw.local.tools.pandora_service import BEHAVIOR_GRAPHS_RESOURCE_ID

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sky_claw.app.db.locks import DistributedLockManager

logger = logging.getLogger(__name__)

#: Agente del reconciliador — distinto del de cada ritual, para que el lock que
#: toma para barrer no se confunda con el del ritual en sí (espejo de U-03).
_RECONCILE_AGENT_ID = "rollback-backup-reconciler"

#: TTL corto: el lock solo cubre un scan + rename/rmtree. Si el proceso muere con
#: él tomado, expira solo y no wedgea el próximo ritual.
_RECONCILE_TTL_SECONDS = 60.0

#: Sufijo que ``DirectoryRollback.__aenter__`` le pone al directorio movido aparte:
#: ``.rollback-<time.time_ns()>``.
#:
#: Se exigen **≥12 dígitos** a propósito. Aunque cada productor declara destinos
#: exactos, sus padres son directorios que pueden contener artefactos del usuario;
#: el piso distingue nuestros nonces de un backup corto con el mismo basename.
#: ``time_ns()`` devuelve 19 dígitos (y ≥16 desde 1970), así que el piso no deja
#: afuera ningún backup real y vuelve el falso positivo prácticamente imposible.
_SUFIJO_MOVE_ASIDE = re.compile(r"\.rollback-\d{12,}$")

#: Nombre del backup que ``ProfileSandbox._apply_changes`` crea dentro del clon
#: antes de tocar el árbol real (fase 0).
_PREFIJO_BACKUP_PROMOTE = "rollback-"

#: Forma de un clon de ``ProfileSandbox.clone()``: ``<perfil>-<12 hex>``. El GC
#: solo toca lo que matchea — el ``.skyclaw_sandbox`` puede tener cosas que el
#: operador dejó ahí, y un reconciliador no es un ``rm -rf`` del directorio.
_NOMBRE_DE_CLON = re.compile(r"^.+-[0-9a-f]{12}$")

#: Un clon sobrevive al ritual **a propósito**: espera la decisión HITL del
#: operador, y en esa ventana NINGÚN lock lo protege (el del ritual ya se liberó
#: al terminar la corrida). Sin esta gracia, arrancar una segunda instancia de
#: Sky-Claw mientras hay una aprobación pendiente le borraría el clon a la
#: primera. 24 h es deliberadamente holgado: lo que se recupera es disco, no
#: corrección, y el costo de equivocarse es re-correr el ritual.
VENTANA_DE_GRACIA_SEGUNDOS: float = 24 * 60 * 60

#: Lock del ritual que produce clones bajo ``.skyclaw_sandbox/``. El único que
#: corre sandboxeado hoy es Synthesis ("corre SIEMPRE en sandbox", tool_dispatcher).
LOCK_DEL_SANDBOX = "Synthesis.esp"

#: Nombres de productor que el cableado de producción construye
#: (:func:`construir_productores_de_move_aside`). El ancla de
#: ``tests/test_rollback_reconciler.py`` exige que todo módulo que use
#: ``DirectoryRollback`` esté mapeado a uno de estos.
PRODUCTORES_CABLEADOS: frozenset[str] = frozenset({"dyndolod", "pandora", "bodyslide"})


@dataclass(frozen=True, slots=True)
class ProductorDeMoveAside:
    """Un ritual que deja residuo ``<dir>.rollback-<nonce>``, con qué y bajo qué lock.

    **Por qué los tres datos viajan juntos.** La primera versión de este módulo
    asumía que había *una* familia move-aside: una raíz (``<mo2>/mods``) y un lock
    (``dyndolod-pipeline``). Las dos suposiciones son propiedades de DynDOLOD, no
    del mecanismo — Pandora mueve aparte su único ``Pandora_Output`` administrado,
    que cuelga del juego y se serializa con ``behavior-graphs``.
    Barrer su residuo mirando el lock de DynDOLOD sería peor que no barrerlo:
    restauraría un backup mientras el ritual que lo creó sigue corriendo.

    Enunciado como propiedad del mecanismo: *cada residuo se reconcilia bajo el
    lock del ritual que lo produjo, y solo si corresponde a un destino exacto que
    ese ritual regenera por completo.* Un productor nuevo declara ambas cosas o
    no se barre. Declarar solo el directorio padre daría autoridad sobre siblings
    ajenos que compartan la forma ``*.rollback-<nonce>``.
    """

    #: Identificador corto, para logs y para el reporte de omitidos.
    nombre: str
    #: Lock del ritual: mientras esté vivo, su residuo es legítimo y no se toca.
    lock_resource_id: str
    #: Directorios exactos que el ritual regenera y puede restaurar.
    destinos: tuple[pathlib.Path, ...]


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """Qué hizo el reconciliador. Todos los campos son observables en disco."""

    #: Directorios devueltos a su lugar desde su backup move-aside.
    restaurados: tuple[pathlib.Path, ...] = ()
    #: Residuo descartado por ser copia redundante probada (GC de disco).
    descartados: tuple[pathlib.Path, ...] = ()
    #: Backups que NO se tocaron: son la única ruta de recuperación y el
    #: filesystem no alcanza para decidir por el operador.
    preservados: tuple[pathlib.Path, ...] = ()
    #: Familias salteadas porque su ritual está en curso (posiblemente en otra
    #: instancia). No es un error: es el guard funcionando.
    omitidos_por_lock: tuple[str, ...] = ()


@dataclass
class _Acumulador:
    restaurados: list[pathlib.Path] = field(default_factory=list)
    descartados: list[pathlib.Path] = field(default_factory=list)
    preservados: list[pathlib.Path] = field(default_factory=list)
    omitidos: list[str] = field(default_factory=list)

    def cerrar(self) -> ReconcileOutcome:
        return ReconcileOutcome(
            restaurados=tuple(self.restaurados),
            descartados=tuple(self.descartados),
            preservados=tuple(self.preservados),
            omitidos_por_lock=tuple(self.omitidos),
        )


async def reconcile_orphan_rollback_backups(
    *,
    productores: Sequence[ProductorDeMoveAside],
    sandbox_root: pathlib.Path | None,
    lock_manager: DistributedLockManager,
) -> ReconcileOutcome:
    """Reconcilia el residuo de rollback de una muerte dura previa (U-08 mitad 2).

    Idempotente por construcción: cada acción borra su propio disparador (el
    backup restaurado deja de existir, el clon descartado también), y lo que se
    preserva se vuelve a reportar en el próximo arranque — que es lo deseado, no
    un efecto colateral: el operador debe seguir viendo que hay algo pendiente.

    Args:
        productores: Rituales que dejan residuo move-aside, cada uno con sus
            destinos exactos y su lock (ver :class:`ProductorDeMoveAside`). Lista vacía →
            no se barre ninguno. Construir con
            :func:`construir_productores_de_move_aside`.
        sandbox_root: ``<mo2>/.skyclaw_sandbox``. ``None`` → no-op.
        lock_manager: El MISMO que serializa los rituales, para que el guard de
            concurrencia mire los locks reales y no una copia.

    Returns:
        El :class:`ReconcileOutcome` con lo que se hizo y lo que se dejó.
    """
    acc = _Acumulador()
    for productor in productores:
        await _bajo_el_lock_del_ritual(
            nombre=productor.nombre,
            resource_id=productor.lock_resource_id,
            lock_manager=lock_manager,
            acc=acc,
            accion=functools.partial(_reconciliar_move_aside, productor.destinos, acc),
        )
    if sandbox_root is not None:
        await _bajo_el_lock_del_ritual(
            nombre="clon-sandbox",
            resource_id=LOCK_DEL_SANDBOX,
            lock_manager=lock_manager,
            acc=acc,
            accion=functools.partial(_reconciliar_clones_sandbox, sandbox_root, acc),
        )
    return acc.cerrar()


def construir_productores_de_move_aside(
    *,
    mo2_root: pathlib.Path | None,
    game: pathlib.Path | None = None,
) -> list[ProductorDeMoveAside]:
    """Los productores reales, con destinos exactos — fuente única del cableado.

    Vive acá y no en ``app_context`` para que el conjunto de rituales barridos sea
    testeable sin levantar la app, y para que agregar uno sea un cambio en un solo
    lugar. ``PRODUCTORES_CABLEADOS`` enumera los nombres que esta función puede
    devolver; el ancla exige que todo usuario de ``DirectoryRollback`` mapee a uno.

    Un productor cuyos destinos no resuelven se omite —no se inventa una ruta— y
    el resto barre igual.
    """
    productores: list[ProductorDeMoveAside] = []
    # DynDOLOD/TexGen mueven aparte TRES destinos, y no todos cuelgan de la misma
    # raíz: los dos mods de salida empaquetados viven bajo `<mo2>/mods`, y el
    # staging crudo de TexGen bajo la raíz administrada del juego. Las constantes
    # son las mismas que consume `dyndolod_service` al construir sus
    # `DirectoryRollback`; declarar el padre daría autoridad sobre otros
    # directorios con un sufijo de rollback válido.
    destinos_dyndolod: list[pathlib.Path] = []
    if mo2_root is not None:
        mods = mo2_root / "mods"
        destinos_dyndolod += [
            mods / DynDOLODRunner.DYNDOLLOD_MOD_NAME,
            mods / DynDOLODRunner.TEXGEN_MOD_NAME,
        ]
    # El staging crudo entró al move-aside con el fix B del review de #493, y sin
    # declararlo acá su residuo quedaría fuera del barrido: tras una muerte dura,
    # el backup del staging bajo la raíz administrada es la ÚNICA copia del árbol
    # previo. La raíz se deriva de la MISMA función que usa el servicio —igual que
    # Pandora— para que un cambio del destino administrado no haya que replicarlo
    # acá (hermano del defecto #388).
    raiz_administrada = dyndolod_output_target(game=game)
    if raiz_administrada is not None:
        destinos_dyndolod.append(raiz_administrada / DynDOLODRunner.TEXGEN_OUTPUT_NAME)
    if destinos_dyndolod:
        productores.append(
            ProductorDeMoveAside(
                nombre="dyndolod",
                lock_resource_id="dyndolod-pipeline",
                destinos=tuple(destinos_dyndolod),
            )
        )

    # Pandora mueve aparte su único `Pandora_Output` administrado, que NO cuelga
    # de `<mo2>/mods` sino del juego. El reconciliador deriva la raíz de la MISMA
    # función que usa el servicio: si cambia el destino administrado, el barrido lo
    # sigue sin duplicar el cálculo (hermano del defecto #388).
    output_pandora = pandora_output_target(game=game)
    if output_pandora is not None:
        productores.append(
            ProductorDeMoveAside(
                nombre="pandora",
                lock_resource_id=BEHAVIOR_GRAPHS_RESOURCE_ID,
                destinos=(output_pandora,),
            )
        )

    # BodySlide, a diferencia de DynDOLOD/Pandora, no tiene un nombre de
    # destino fijo: el LLM elige `group` en cada corrida, así que no hay una
    # lista que declarar de antemano. `BodySlide_Output` sí es una raíz
    # EXCLUSIVA de Sky-Claw (ver `output_targets.bodyslide_output_root`), así
    # que sus destinos se DESCUBREN escaneando los backups `.rollback-*` que
    # ya existen, en vez de declararse — sin el riesgo de "autoridad sobre
    # siblings ajenos" que correría hacerlo sobre un padre COMPARTIDO como
    # `mods/` o `game/`.
    raiz_bodyslide = bodyslide_output_root(game=game)
    if raiz_bodyslide is not None:
        productores.append(
            ProductorDeMoveAside(
                nombre="bodyslide",
                lock_resource_id=BODYSLIDE_MESHES_RESOURCE_ID,
                destinos=_destinos_bodyslide_por_escaneo(raiz_bodyslide),
            )
        )
    return productores


def _destinos_bodyslide_por_escaneo(raiz: pathlib.Path) -> tuple[pathlib.Path, ...]:
    """Backups huérfanos YA presentes bajo la raíz exclusiva de BodySlide.

    ``raiz`` puede no existir todavía (ningún run corrió) — no es un error,
    simplemente no hay nada que reconciliar. Solo cuentan los hijos DIRECTOS
    cuyo nombre termina en el sufijo de move-aside; un grupo ya commiteado
    (sin backup pendiente) no es un destino.
    """
    if not raiz.is_dir():
        return ()
    # Un hijo cuyo nombre ENTERO es el sufijo (p.ej. ".rollback-123456789012",
    # nada antes del punto) deja basename vacío tras el ``sub`` — y
    # ``with_name("")`` levanta ``ValueError`` (Copilot, PR #430). No es un
    # destino real: se ignora en vez de tirar abajo el constructor entero.
    destinos = {
        hijo.with_name(basename)
        for hijo in raiz.iterdir()
        if _SUFIJO_MOVE_ASIDE.search(hijo.name) and (basename := _SUFIJO_MOVE_ASIDE.sub("", hijo.name))
    }
    return tuple(sorted(destinos))


async def _bajo_el_lock_del_ritual(
    *,
    nombre: str,
    resource_id: str,
    lock_manager: DistributedLockManager,
    acc: _Acumulador,
    accion: Callable[[], Awaitable[None]],
) -> None:
    """Corre ``accion`` bajo el lock del ritual que produce ese residuo.

    Guard idéntico al de ``reconcile_orphan_precache_flag`` (U-03), y por el mismo
    motivo: sin adquirir el lock hay un TOCTOU real — entre "no veo el lock" y el
    ``rename``/``rmtree``, otra instancia puede arrancar el ritual y crear el
    backup que este proceso destruiría. El ``get_lock_info`` previo es solo un
    fast-path para no pagar el backoff de ``acquire_lock`` cuando ya hay dueño.

    Es **por productor**, no global: un ritual de Pandora en curso no debe frenar
    el barrido del residuo de DynDOLOD, y —sobre todo— el residuo de Pandora no
    puede barrerse mirando el lock de DynDOLOD.
    """
    info = await lock_manager.get_lock_info(resource_id)
    if info is not None and not info.is_expired:
        logger.info(
            "Reconciliación de '%s' salteada: el lock '%s' sigue vivo (ritual en curso).",
            nombre,
            resource_id,
        )
        acc.omitidos.append(nombre)
        return

    try:
        await lock_manager.acquire_lock(resource_id, _RECONCILE_AGENT_ID, ttl=_RECONCILE_TTL_SECONDS)
    except LockAcquisitionError:
        logger.info(
            "Reconciliación de '%s' salteada: no se pudo adquirir '%s' (ritual activo).",
            nombre,
            resource_id,
        )
        acc.omitidos.append(nombre)
        return

    try:
        await accion()
    finally:
        await lock_manager.release_lock(resource_id, _RECONCILE_AGENT_ID)


async def _reconciliar_move_aside(destinos: Sequence[pathlib.Path], acc: _Acumulador) -> None:
    """Familia ``<dir>.rollback-<nonce>``: el marcador durable es el target.

    - **Target ausente** → el run murió entre el move-aside y la regeneración; el
      backup es el último estado bueno. Se restaura: es literalmente lo que hace
      ``DirectoryRollback._restore_backup`` en su camino de excepción.
    - **Target presente** → el tool alcanzó a escribir salida nueva y el run no
      llegó a commitear (si hubiera commiteado, ``DirectoryRollback.commit()``
      habría descartado el backup). No hay forma de saber desde el filesystem si
      esa salida está completa, y las dos acciones posibles destruyen trabajo:
      borrar el backup tira la generación anterior, pisar el target tira la nueva.
      Se preservan ambos y se avisa.
    """
    for backup in await asyncio.to_thread(_listar_backups_move_aside, destinos):
        destino = backup.with_name(_SUFIJO_MOVE_ASIDE.sub("", backup.name))
        # ``path_present`` y no ``exists``: si el destino es un enlace ROTO,
        # ``exists()`` da False y caeríamos al ``rename`` de abajo, que **reemplaza**
        # el enlace en silencio — destruyendo algo del usuario que este
        # reconciliador no tiene por qué tocar. "Hay algo acá" es la pregunta
        # correcta, y la respuesta segura es preservar los dos y avisar.
        if await asyncio.to_thread(path_present, destino):
            logger.warning(
                "Backup de rollback huérfano en '%s' CON el destino '%s' ya presente: "
                "no se toca ninguno (la salida nueva puede estar incompleta y el backup "
                "es la única copia de la anterior). Resolución manual.",
                backup,
                destino,
            )
            acc.preservados.append(backup)
            continue
        try:
            await asyncio.to_thread(backup.rename, destino)
        except OSError:
            logger.warning("No se pudo restaurar el backup huérfano '%s' a '%s'", backup, destino, exc_info=True)
            acc.preservados.append(backup)
            continue
        logger.info(
            "Restaurado '%s' desde el backup huérfano de una muerte dura previa: "
            "el ritual no llegó a regenerarlo y este era el último estado bueno.",
            destino,
        )
        acc.restaurados.append(destino)


def _listar_backups_move_aside(destinos: Sequence[pathlib.Path]) -> list[pathlib.Path]:
    """Backups de los destinos exactos del productor, sin repetir.

    Cada parent puede contener residuos de otros componentes. Solo entra un hijo
    real cuyo basename, tras quitar un sufijo válido, coincide exactamente con el
    nombre del destino declarado. No se usa glob: nombres con metacaracteres no
    amplían el alcance. Destinos y backups se deduplican defensivamente.
    """
    destinos_vistos: set[pathlib.Path] = set()
    vistos: set[pathlib.Path] = set()
    backups: list[pathlib.Path] = []
    for destino in destinos:
        if destino in destinos_vistos:
            continue
        destinos_vistos.add(destino)
        raiz = destino.parent
        if not raiz.is_dir():
            continue
        for hijo in sorted(raiz.iterdir()):
            sufijo = _SUFIJO_MOVE_ASIDE.search(hijo.name)
            if sufijo is None or _SUFIJO_MOVE_ASIDE.sub("", hijo.name) != destino.name:
                continue
            # ``is_dir()`` **sigue** el enlace, así que un symlink/junction a
            # directorio llamado ``X.rollback-<nonce>`` entraba como backup legítimo
            # y el ``backup.rename(destino)`` de abajo lo movía encima del target
            # real: el target quedaba siendo un enlace a un árbol ajeno, reportado
            # como "restaurado desde el último estado bueno". Los parents que se
            # inspeccionan incluyen directorios del USUARIO (el juego para Pandora),
            # no sólo los nuestros, así que el impostor no es hipotético.
            #
            # Este módulo es el que NO tiene el fail-closed de
            # ``DirectoryRollback.__aenter__``: opera sobre lo que encuentra en disco
            # al arrancar, así que es el único camino por el que un artefacto
            # enlazado alcanza una operación destructiva.
            if is_link(hijo):
                logger.warning(
                    "Se ignora '%s': tiene nombre de backup de rollback pero es un enlace, "
                    "no un directorio real. Un backup legítimo lo produce un rename O(1) de "
                    "%s, que nunca deja un enlace.",
                    hijo,
                    "DirectoryRollback",
                )
                continue
            if hijo.is_dir() and hijo not in vistos:
                vistos.add(hijo)
                backups.append(hijo)
    return backups


async def _reconciliar_clones_sandbox(sandbox_root: pathlib.Path, acc: _Acumulador) -> None:
    """Familia ``.skyclaw_sandbox/<clon>``: el marcador durable es su ``rollback-*``.

    - **Con ``rollback-*``** → ``_apply_changes`` llegó a su fase 0 (backup) y no
      llegó a limpiarla: el perfil real puede haber quedado medio-aplicado y ese
      directorio es la ÚNICA ruta de recuperación manual — el propio mensaje de
      ``SandboxRollbackError`` apunta ahí. Se preserva SIEMPRE.
    - **Sin ``rollback-*``** → nada se promovió, así que el clon es una copia del
      árbol real más la salida de un ritual que ya nadie va a aprobar: puro leak
      de disco (varios GB por corrida). Es el piso de GC que pide U-08.

    La ventana de gracia protege el caso legítimo que NO tiene lock: un clon
    esperando la aprobación HITL del operador.
    """
    ahora = time.time()
    for clon in await asyncio.to_thread(_listar_clones, sandbox_root):
        backup_de_promote = await asyncio.to_thread(_backup_de_promote, clon)
        if backup_de_promote is not None:
            logger.warning(
                "Clon de sandbox '%s' con un backup de promote a medio aplicar ('%s'): "
                "el perfil real pudo quedar inconsistente. NO se descarta — es la única "
                "ruta de recuperación manual.",
                clon,
                backup_de_promote,
            )
            acc.preservados.append(backup_de_promote)
            continue
        edad = ahora - await asyncio.to_thread(_mtime, clon)
        if edad < VENTANA_DE_GRACIA_SEGUNDOS:
            logger.debug("Clon de sandbox '%s' dentro de la ventana de gracia (%.0fs); no se toca.", clon, edad)
            continue
        try:
            # `limpiar_readonly`: el clon sale de `shutil.copytree(...,
            # copy_function=shutil.copy2)` sobre el perfil real, y `copy2`
            # propaga el modo de la fuente — un mod extraído de un archive
            # read-only produce un clon read-only. Sin el flag, este descarte
            # falla en silencio (el `except` de abajo lo traga) y el clon de
            # varios GB queda huérfano, que es justo el leak que este
            # reconciliador existe para cerrar (U-08).
            await asyncio.to_thread(rmtree_link_aware, clon, limpiar_readonly=True)
        except OSError:
            logger.warning("No se pudo descartar el clon de sandbox huérfano '%s'", clon, exc_info=True)
            continue
        if await asyncio.to_thread(clon.exists):
            logger.warning(
                "El clon de sandbox huérfano '%s' sobrevivió al descarte; queda para el próximo arranque.", clon
            )
            continue
        logger.info(
            "Clon de sandbox huérfano '%s' descartado (%.1f h sin actividad y sin promote pendiente).",
            clon,
            edad / 3600,
        )
        acc.descartados.append(clon)


def _mtime(ruta: pathlib.Path) -> float:
    """Función con nombre en vez de un ``lambda``: el lambda capturaba la variable
    del bucle por referencia (ruff B023) y una evaluación diferida habría medido la
    edad del clon equivocado."""
    return ruta.stat().st_mtime


def _listar_clones(sandbox_root: pathlib.Path) -> list[pathlib.Path]:
    """Clones candidatos a descarte, **excluyendo enlaces**.

    Un enlace acá no puede ser un clon legítimo: ``ProfileSandbox.clone()`` los
    crea copiando, nunca enlazando.

    Este filtro cubre la RAÍZ; el borrado usa ``rmtree_link_aware``, que cubre
    además cualquier enlace **anidado**. Hasta que esa primitiva existió, el
    descarte era ``shutil.rmtree(clon, True)``: sobre un junction ``rmtree`` no
    falla —lo atraviesa y borra el árbol destino— y el ``ignore_errors``
    garantizaba que nadie se enterara. Este módulo nació nombrando ese modo de
    falla en su propio docstring y descartando con la llamada que lo tenía.
    """
    if not sandbox_root.is_dir():
        return []
    return sorted(
        hijo
        for hijo in sandbox_root.iterdir()
        if not is_link(hijo) and hijo.is_dir() and _NOMBRE_DE_CLON.match(hijo.name)
    )


def _backup_de_promote(clon: pathlib.Path) -> pathlib.Path | None:
    """Primer ``rollback-*`` dentro del clon, o ``None`` si el promote no arrancó."""
    for hijo in sorted(clon.iterdir()):
        if hijo.is_dir() and hijo.name.startswith(_PREFIJO_BACKUP_PROMOTE):
            return hijo
    return None
