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
import shutil
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sky_claw.app.db.locks import LockAcquisitionError
from sky_claw.app.security.links import is_link, path_present
from sky_claw.local.tools.output_targets import pandora_rollback_dirs
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
#: Se exigen **≥12 dígitos** a propósito. Con una sola raíz bajo `<mo2>/mods` el
#: patrón laxo (``\d+``) era inofensivo, pero desde que se barre también la raíz del
#: juego y el dir del ejecutable de Pandora —directorios del usuario, no nuestros—
#: un ``Algo.rollback-1`` ajeno entraría al barrido y podría terminar renombrado.
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
PRODUCTORES_CABLEADOS: frozenset[str] = frozenset({"dyndolod", "pandora"})


@dataclass(frozen=True, slots=True)
class ProductorDeMoveAside:
    """Un ritual que deja residuo ``<dir>.rollback-<nonce>``, con dónde y bajo qué lock.

    **Por qué los tres datos viajan juntos.** La primera versión de este módulo
    asumía que había *una* familia move-aside: una raíz (``<mo2>/mods``) y un lock
    (``dyndolod-pipeline``). Las dos suposiciones son propiedades de DynDOLOD, no
    del mecanismo — Pandora mueve aparte sus ``Pandora_Output``, que cuelgan del
    juego y del dir de su ejecutable, y se serializa con ``behavior-graphs``.
    Barrer su residuo mirando el lock de DynDOLOD sería peor que no barrerlo:
    restauraría un backup mientras el ritual que lo creó sigue corriendo.

    Enunciado como propiedad del mecanismo: *cada residuo se reconcilia bajo el
    lock del ritual que lo produjo, y solo bajo las raíces donde ese ritual
    escribe.* Un productor nuevo declara ambas cosas o no se barre.
    """

    #: Identificador corto, para logs y para el reporte de omitidos.
    nombre: str
    #: Lock del ritual: mientras esté vivo, su residuo es legítimo y no se toca.
    lock_resource_id: str
    #: Directorios PADRE donde buscar los ``<dir>.rollback-<nonce>``.
    raices: tuple[pathlib.Path, ...]


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
            raíces y su lock (ver :class:`ProductorDeMoveAside`). Lista vacía →
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
            accion=functools.partial(_reconciliar_move_aside, productor.raices, acc),
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
    pandora_exe: pathlib.Path | None = None,
) -> list[ProductorDeMoveAside]:
    """Los productores reales, con sus raíces resueltas — fuente única del cableado.

    Vive acá y no en ``app_context`` para que el conjunto de rituales barridos sea
    testeable sin levantar la app, y para que agregar uno sea un cambio en un solo
    lugar. ``PRODUCTORES_CABLEADOS`` enumera los nombres que esta función puede
    devolver; el ancla exige que todo usuario de ``DirectoryRollback`` mapee a uno.

    Un productor cuyas raíces no resuelven se omite —no se inventa una ruta— y el
    resto barre igual.
    """
    productores: list[ProductorDeMoveAside] = []
    if mo2_root is not None:
        # DynDOLOD/TexGen mueven aparte sus mods de salida empaquetados, que por
        # construcción cuelgan de `<mo2>/mods` (`dyndolod_service`: `mods_path /
        # runner.DYNDOLLOD_MOD_NAME`).
        productores.append(
            ProductorDeMoveAside(
                nombre="dyndolod",
                lock_resource_id="dyndolod-pipeline",
                raices=(mo2_root / "mods",),
            )
        )

    # Pandora mueve aparte sus `Pandora_Output`, que NO cuelgan de `<mo2>/mods`: el
    # `pandora_service` los toma de `output_targets.pandora_rollback_dirs`, o sea el
    # juego (su `cwd`) y el dir de su ejecutable. Se derivan de esa MISMA función y
    # no de una lista escrita acá: si el rollback gana o pierde una raíz, el barrido
    # la sigue sin que nadie tenga que acordarse — es el hermano de #388, donde el
    # sondeo de permisos y la búsqueda de salida divergieron por tener dos fuentes.
    raices_pandora = tuple(dict.fromkeys(d.parent for d in pandora_rollback_dirs(game=game, exe=pandora_exe)))
    if raices_pandora:
        productores.append(
            ProductorDeMoveAside(
                nombre="pandora",
                lock_resource_id=BEHAVIOR_GRAPHS_RESOURCE_ID,
                raices=raices_pandora,
            )
        )
    return productores


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


async def _reconciliar_move_aside(raices: Sequence[pathlib.Path], acc: _Acumulador) -> None:
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
    for backup in await asyncio.to_thread(_listar_backups_move_aside, raices):
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


def _listar_backups_move_aside(raices: Sequence[pathlib.Path]) -> list[pathlib.Path]:
    """Backups bajo cualquiera de las raíces del productor, sin repetir.

    Se deduplica porque dos raíces pueden coincidir en una instalación real (p. ej.
    Pandora instalado dentro del directorio del juego), y restaurar dos veces el
    mismo backup haría que el segundo intento fallara con el target ya presente.
    """
    vistos: set[pathlib.Path] = set()
    backups: list[pathlib.Path] = []
    for raiz in raices:
        if not raiz.is_dir():
            continue
        for hijo in sorted(raiz.iterdir()):
            # ``is_dir()`` **sigue** el enlace, así que un symlink/junction a
            # directorio llamado ``X.rollback-<nonce>`` entraba como backup legítimo
            # y el ``backup.rename(destino)`` de abajo lo movía encima del target
            # real: el target quedaba siendo un enlace a un árbol ajeno, reportado
            # como "restaurado desde el último estado bueno". Las raíces que se
            # barren incluyen directorios del USUARIO (la del juego y la del exe de
            # Pandora), no sólo las nuestras, así que el impostor no es hipotético.
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
            if hijo.is_dir() and _SUFIJO_MOVE_ASIDE.search(hijo.name) and hijo not in vistos:
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
            await asyncio.to_thread(shutil.rmtree, clon, True)
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

    Un clon se descarta con ``shutil.rmtree(clon, True)`` —con
    ``ignore_errors=True``, así que un fallo no se ve. Sobre un junction de Windows
    ``rmtree`` no falla: lo atraviesa y borra el árbol destino, y el
    ``ignore_errors`` garantiza que nadie se entere. Un enlace acá no puede ser un
    clon legítimo: ``ProfileSandbox.clone()`` los crea copiando, nunca enlazando.
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
