"""Rollback O(1) para un directorio regenerado por completo (move-aside).

Para herramientas que regeneran un directorio entero (DynDOLOD/TexGen Output,
potencialmente varios GB), copiar el árbol antes de cada run es caro y choca con
el límite del snapshot store copy-based. Este context manager preserva el estado
previo renombrando el directorio a un sibling adyacente (mismo padre = misma
unidad = O(1)) y lo restaura si el bloque falla — rollback real byte-a-byte sin
copiar datos.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import pathlib
import time
from collections.abc import Callable
from typing import Any

from sky_claw.app.security.links import link_kind_or_raise, path_present, rmtree_link_aware

logger = logging.getLogger("SkyClaw.DirectoryRollback")

#: Reintentos ante fallos transitorios de FS en Windows (WinError 5/32): renombrar
#: o borrar un directorio recién tocado puede fallar por handles del AV/indexer que
#: se liberan en milisegundos. Se reintenta con backoff corto antes de rendirse.
_FS_RETRIES = 5
_FS_BACKOFF_SECONDS = 0.1


async def _borrar_arbol_o_enlace(ruta: pathlib.Path) -> None:
    """Descarta *ruta*, sea un árbol real o un enlace, sin tocar ningún destino ajeno.

    Delega en :func:`~sky_claw.app.security.links.rmtree_link_aware`, que aplica
    la misma decisión (enlace → borrar sólo el enlace; real → recursar) en la
    raíz Y en cada nivel anidado. El recorrido nació acá y se promovió a
    ``app.security.links`` cuando el censo mostró que otros 8 módulos borraban
    árboles con ``shutil.rmtree`` crudo: era la única copia correcta del paquete
    y estaba privada dentro de un módulo privado.

    Sin retry por entrada a propósito: el recorrido entero corre dentro de UN
    ``_fs_op_with_retry``, así que un ``OSError`` transitorio reintenta todo —
    idempotente, porque lo ya borrado no vuelve a aparecer en el ``scandir``.

    ``limpiar_readonly`` queda apagado: los artefactos de rollback los produce
    este módulo con un ``rename``, así que heredan los permisos del árbol que la
    herramienta generó. Si alguno viniera de solo-lectura, el ``OSError`` tiene
    que verse en vez de que se muten permisos en silencio.
    """
    await _fs_op_with_retry(rmtree_link_aware, ruta)


async def _fs_op_with_retry(op: Callable[..., Any], *args: Any) -> Any:
    """Ejecuta una op de FS off-loop, reintentando ante ``OSError`` transitorio.

    En Windows, ``rename``/``rmtree`` sobre un directorio recién creado puede
    devolver ``WinError 5``/``32`` porque el AV/indexer aún tiene un handle; suele
    resolverse en milisegundos. Se reintenta ``_FS_RETRIES`` veces con backoff
    lineal; si persiste, propaga el último ``OSError`` (fail-closed en el caller).
    """
    last_exc: OSError | None = None
    for attempt in range(_FS_RETRIES):
        try:
            return await asyncio.to_thread(op, *args)
        except OSError as exc:
            last_exc = exc
            if attempt < _FS_RETRIES - 1:
                await asyncio.sleep(_FS_BACKOFF_SECONDS * (attempt + 1))
    assert last_exc is not None  # el loop siempre corre al menos una vez
    raise last_exc


#: Referencias fuertes a las tasks en vuelo tras una cancelación: el loop solo
#: guarda referencias DÉBILES, así que sin esto el GC podría recolectar la task
#: del rename —o la de su deshacer— antes de que termine (mismo motivo que
#: ``sandbox_run._background_tasks``).
_background_tasks: set[asyncio.Task[Any]] = set()


def _track(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    """Ancla ``task`` contra GC hasta que termine (ver ``_background_tasks``)."""
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _deshacer_rename_cancelado(
    rename_task: asyncio.Task[Any],
    backup: pathlib.Path,
    target: pathlib.Path,
) -> None:
    """Observa el desenlace REAL de un move-aside cancelado y lo revierte.

    Si el rename terminó en excepción no movió nada. Si terminó bien, el dir
    previo quedó bajo el nombre de backup **sin dueño**: el context nunca se
    registró en el ``AsyncExitStack`` del caller, así que no habrá ``__aexit__``
    que lo restaure. Devolverlo a su lugar acá es lo único que evita que una
    cancelación bien temporizada haga desaparecer la salida previa sin que
    ninguna herramienta llegara a correr.
    """
    try:
        await asyncio.shield(rename_task)
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 — sólo interesa SI movió, no por qué falló
        # Cualquier desenlace que no sea "terminó bien" significa que el rename no
        # dejó el dir bajo el nombre de backup: no hay nada que deshacer. Se listan
        # las dos porque CancelledError deriva de BaseException, no de Exception.
        return
    with contextlib.suppress(OSError):
        if await asyncio.to_thread(backup.exists) and not await asyncio.to_thread(target.exists):
            await _fs_op_with_retry(backup.rename, target)
            logger.warning("Move-aside cancelado: '%s' devuelto a su lugar desde '%s'", target, backup)


class DirectoryRollback:
    """Move-aside rollback para un directorio regenerado por completo.

    Al entrar renombra ``target_dir`` a ``<name>.rollback-<nonce>`` (si existe).
    Al salir:

    - en **excepción** → borra el parcial nuevo y restaura el backup;
    - en **éxito** → descarta el backup (solo libera espacio).

    El ``nonce`` (``time.time_ns()``) evita colisión con backups previos. Todas
    las operaciones de filesystem se delegan a ``asyncio.to_thread`` (con retry
    ante fallos transitorios de Windows) para no bloquear el event loop. La
    restauración es best-effort con logging: nunca lanza desde ``__aexit__``, así
    no enmascara la excepción original del body.
    """

    def __init__(
        self,
        target_dir: pathlib.Path,
        *,
        enabled: bool = True,
        should_rollback: Callable[[], bool] | None = None,
    ) -> None:
        self._target = target_dir
        self._enabled = enabled
        #: Veto consultado JUSTO ANTES de restaurar (review Codex #399). Existe
        #: porque el move-aside vive anidado DENTRO de un
        #: ``SnapshotTransactionLock`` y sale ANTES que él: si el heartbeat perdió
        #: el lease durante el run, el lock saltea su propio rollback —"never
        #: clobber a concurrent owner's mutations", ``locks.py``— pero este context
        #: ya restauró, borrando la salida del nuevo dueño y devolviendo un backup
        #: viejo. El caller cablea ``lambda: not lock.lease_lost`` para que las dos
        #: capas apliquen el MISMO criterio.
        self._should_rollback = should_rollback
        self._backup: pathlib.Path | None = None
        #: M-7: refleja si el rollback en el path de excepción se COMPLETÓ. El
        #: restore es best-effort (traga OSError sin re-raise), así que el caller no
        #: puede confiar en "hubo excepción ⇒ rolled_back=True". Este flag distingue
        #: un restore exitoso de un fallo silencioso de rmtree/rename que dejó el
        #: output parcial en disco. Espeja SnapshotTransactionLock.rollback_completed.
        self.rollback_completed: bool = False

    @property
    def target(self) -> pathlib.Path:
        """Directorio protegido — para que el caller lo nombre en sus logs sin
        tener que alcanzar el atributo privado."""
        return self._target

    async def __aenter__(self) -> DirectoryRollback:
        if not self._enabled:
            return self
        # Fail-closed ante un target ENLAZADO, antes de mover o borrar nada. El
        # move-aside renombraría el enlace y no el árbol al que apunta, así que el
        # backup quedaría siendo un enlace y las dos operaciones destructivas de
        # ``__aexit__`` dejarían de significar lo que dicen: ``rmtree`` lanza sobre
        # un symlink (y ``__aexit__`` traga ese OSError por diseño, dejando el
        # backup huérfano y el run "exitoso") y ATRAVIESA un junction de Windows,
        # borrando el contenido del destino — datos de otro, en el camino de éxito.
        # El rollback prometido es inejecutable sobre un enlace, así que no se
        # corre: mismo criterio que el rename fallido de más abajo y que
        # ``grass_profile`` ante un mod que ya existe como symlink.
        #
        # Se pregunta por el enlace ANTES que por la existencia a propósito
        # (``file_permissions.restrict_to_owner`` documenta el mismo orden): para un
        # enlace ROTO ``exists()`` da False, así que el orden inverso tomaba el
        # camino "primer run, nada que preservar" y la ruta seguía ocupada — el
        # ``mkdir()`` posterior de la herramienta moría con un ``FileExistsError``
        # que no menciona enlaces y manda a depurar el lugar equivocado.
        #
        # ``link_kind_or_raise`` con retry, no ``link_kind`` pelado (review
        # CodeRabbit #404): un bloqueo TRANSITORIO de AV/indexer acá se leía
        # como "no es un enlace" y dejaba pasar el move-aside — el mismo modo
        # de falla que ``_borrar_arbol_o_enlace`` ya cerró, sin cerrar en este
        # preflight.
        tipo_de_enlace = await _fs_op_with_retry(link_kind_or_raise, self._target)
        if tipo_de_enlace is not None:
            raise OSError(
                f"'{self._target}' es un enlace ({tipo_de_enlace}) y no un directorio real: "
                "el rollback move-aside renombraría el enlace y no su contenido, así que "
                "no puede garantizar la restauración prometida. Reemplazá el enlace por "
                "una carpeta real para correr este ritual con rollback."
            )
        if not await asyncio.to_thread(path_present, self._target):
            # Primer run: no hay estado previo que preservar.
            return self
        backup = self._target.with_name(f"{self._target.name}.rollback-{time.time_ns()}")
        # El rename corre en un hilo (``asyncio.to_thread``): cancelar la corrutina
        # que lo espera NO lo interrumpe. Sin shield, una cancelación acá dejaba el
        # rename completándose en background mientras el caller nunca llegaba a
        # registrar este context — el dir previo quedaba varado bajo un nombre
        # ``.rollback-*`` y ningún ``__aexit__`` podía restaurarlo (review Codex
        # #399). Se espera el desenlace REAL antes de decidir, mismo patrón F2 que
        # ``sandbox_run.run_ritual_in_sandbox`` usa para ``clone()``.
        rename_task = _track(asyncio.ensure_future(_fs_op_with_retry(self._target.rename, backup)))
        try:
            await asyncio.shield(rename_task)
        except asyncio.CancelledError:
            deshacer = _track(asyncio.ensure_future(_deshacer_rename_cancelado(rename_task, backup, self._target)))
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(deshacer)
            raise
        except OSError as exc:
            # Fail-closed: sin move-aside no podemos garantizar el rollback que el
            # caller pidió; abortar antes de correr la operación destructiva.
            raise OSError(f"No se pudo mover '{self._target}' aparte para rollback: {exc}") from exc
        self._backup = backup
        logger.debug("Directorio movido aparte para rollback: %s -> %s", self._target, backup)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        if not self._enabled:
            return
        try:
            if exc_type is not None:
                if not self._veto_permite_restaurar():
                    # Perdimos la exclusividad: otro dueño pudo haber mutado ya, así
                    # que restaurar pisaría SU salida con nuestro backup viejo. El
                    # backup NO se descarta —queda en disco bajo su nombre
                    # ``.rollback-*``— porque es el único ejemplar del estado previo
                    # y el recovery manual lo necesita. ``rollback_completed`` queda
                    # False a propósito: el caller debe dejar la TX PENDIENTE.
                    logger.critical(
                        "Rollback de '%s' OMITIDO: se perdió la exclusividad del recurso. "
                        "El backup queda en '%s' para recuperación manual.",
                        self._target,
                        self._backup,
                    )
                    return
                await self._restore_backup()
                # M-7: sólo aquí el rollback se considera COMPLETADO. Si
                # _restore_backup lanza, no se llega a esta línea y el flag queda
                # False, señalando al caller un rollback fallido.
                self.rollback_completed = True
            else:
                await self._discard_backup(permitir_restore=True)
        except OSError:
            # Nunca enmascarar la excepción del body ni romper un cierre limpio.
            logger.critical(
                "Fallo limpiando/restaurando el rollback de '%s' (exc_body=%s)",
                self._target,
                exc_type.__name__ if exc_type else None,
                exc_info=True,
            )

    async def commit(self) -> None:
        """Confirma el estado nuevo tras un punto de no-retorno (review Codex #312).

        Una vez que la mutación es final (p. ej. el commit del journal ya ocurrió),
        el output NO debe revertirse aunque el bloque salga por excepción — en
        particular una ``CancelledError`` durante trabajo best-effort post-commit
        (informe de vuelo, shutdown). ``commit()`` descarta el backup y DESACTIVA el
        restore, así el ``__aexit__`` posterior es un no-op. Idempotente; el discard
        es best-effort (un backup huérfano solo ocupa espacio, no corrompe).
        """
        if not self._enabled:
            return
        self._enabled = False
        try:
            # permitir_restore=False (review Codex #401): commit() es un punto de
            # no-retorno — un target vacío EN ESE INSTANTE es el estado final que
            # el caller eligió, no algo que la propia limpieza deba revertir. La
            # lógica de "restaurar si está vacío" que _discard_backup ganó en este
            # mismo PR es correcta para el camino limpio SIN commit, pero viola el
            # contrato de commit() si se aplica acá también.
            await self._discard_backup(permitir_restore=False)
        except OSError:
            logger.warning(
                "commit(): no se pudo descartar el backup de '%s' (queda huérfano)", self._target, exc_info=True
            )
        self._backup = None

    def _veto_permite_restaurar(self) -> bool:
        """Evalúa el veto de ``should_rollback`` **fail-closed** (review CodeRabbit #399).

        El callback lo provee el caller y puede lanzar (p. ej. un ``AttributeError``
        si el lock cambia de superficie). Dejarlo escapar rompería la garantía que
        la docstring de la clase promete —*nunca lanza desde ``__aexit__``*— y
        además enmascararía la excepción original del body. Si el veto no se puede
        evaluar, se asume lo conservador: **no restaurar**, y el backup queda en
        disco. Restaurar a ciegas es lo único irreversible de las dos opciones.
        """
        if self._should_rollback is None:
            return True
        try:
            return bool(self._should_rollback())
        except Exception:  # noqa: BLE001 — boundary: el callback es del caller
            logger.critical(
                "El veto de rollback de '%s' falló al evaluarse; se OMITE el restore (fail-closed).",
                self._target,
                exc_info=True,
            )
            return False

    async def _restore_backup(self) -> None:
        """Descarta el parcial nuevo y restaura el directorio original."""
        # ``path_present``/``_borrar_arbol_o_enlace`` en vez de ``exists``/``rmtree``:
        # el fail-closed de ``__aenter__`` filtra el target enlazado en el caso
        # normal, pero no cubre un enlace que aparezca DURANTE el run (la
        # herramienta, u otro proceso, puede dejar uno donde había un directorio).
        # Sobre un enlace, ``rmtree`` lanzaría —quedando el parcial en disco y el
        # restore sin hacer— o atravesaría un junction borrando un árbol ajeno.
        if await asyncio.to_thread(path_present, self._target):
            await _borrar_arbol_o_enlace(self._target)
        if self._backup is not None and await asyncio.to_thread(path_present, self._backup):
            await _fs_op_with_retry(self._backup.rename, self._target)
            logger.warning("Rollback: '%s' restaurado desde backup tras fallo del pipeline", self._target)

    async def _discard_backup(self, *, permitir_restore: bool) -> None:
        """Descarta el backup tras un run exitoso — **salvo que nada lo haya reemplazado**.

        Enunciado como propiedad del mecanismo (review Codex #399): descartar el
        backup sólo es seguro si la herramienta **efectivamente regeneró** el
        directorio. Si el target no existe al salir, la herramienta no escribió
        ahí, y el backup es el ÚNICO ejemplar del estado previo: borrarlo sería
        pérdida de datos disfrazada de limpieza — silenciosa, porque ocurre en el
        camino de ÉXITO, sin excepción que la delate.

        El caso que lo destapó: Pandora tiene dos ``Pandora_Output`` candidatos
        —cuál usa depende de su versión— y el servicio mueve aparte los dos. En una
        instalación real se escribe uno solo, así que el otro se perdía entero tras
        un run exitoso. Vive acá y no en ``pandora_service`` porque la propiedad no
        es de Pandora: cualquier caller que proteja más de un destino candidato
        —o cuya herramienta pueda no producir salida— tiene el mismo agujero.

        ``permitir_restore=False`` lo pide ``commit()`` (review Codex #401): un
        target vacío en el instante del commit es el estado FINAL elegido por el
        caller, no algo que revertir. Con la bandera en False esta función SÓLO
        descarta —nunca restaura— así que el commit nunca se deshace a sí mismo.
        """
        # ``path_present`` y no ``exists``: un backup que quedó siendo un enlace
        # —de una versión anterior a este fail-closed, o encontrado en disco por el
        # reconciliador de arranque— tiene que entrar acá para que lo descarte
        # ``_borrar_arbol_o_enlace``. Con ``exists()`` un enlace ROTO se saltaba
        # entero y quedaba huérfano para siempre.
        if self._backup is None or not await asyncio.to_thread(path_present, self._backup):
            return

        # Inspección del target con retry, separada de ``_sin_regenerar`` (review
        # CodeRabbit #404): ``link_kind`` pelado tragaba un bloqueo TRANSITORIO
        # de AV/indexer y lo leía como "no es un enlace", dejando que un target
        # enlazado con contenido AJENO se contara como "regenerado" — el mismo
        # modo de falla que ``_borrar_arbol_o_enlace`` ya cerró. No hace falta
        # que corra DENTRO de ``_sin_regenerar``: la decisión de ruteo acá no
        # necesita estar fusionada con la mutación, porque
        # ``_restaurar_target_vacio_sync`` vuelve a chequear el enlace, fusionado
        # con SU mutación, justo antes de tocar algo.
        tipo_de_enlace_target = await _fs_op_with_retry(link_kind_or_raise, self._target)

        def _sin_regenerar() -> bool:
            """ "Existe" no es "lo regeneró" (review CodeRabbit #399): una herramienta
            que crea el directorio candidato y no escribe nada dentro dejaba pasar
            el ``rmtree`` del backup — la misma pérdida silenciosa, un paso más
            allá, y plausible justo en el escenario multi-candidato que motivó este
            método.

            También cuenta como "no regenerado" un target que se volvió un enlace
            (review CodeRabbit #404): un symlink/junction a un directorio AJENO
            NO VACÍO hacía que ``exists()``/``iterdir()`` vieran "hay contenido" y
            esta función devolviera ``False`` — control caía derecho al
            ``_borrar_arbol_o_enlace(self._backup)`` incondicional de más abajo,
            descartando la ÚNICA copia del estado previo mientras el enlace ajeno
            seguía ocupando ``self._target`` sin que nadie lo mirara. Empujar la
            decisión a la rama de ``_restaurar_target_vacio_sync`` es lo que la
            deja pasar por SU chequeo de ``link_kind_or_raise`` fail-closed, en vez de
            saltearlo entero.
            """
            return tipo_de_enlace_target is not None or not self._target.exists() or not any(self._target.iterdir())

        if permitir_restore and await asyncio.to_thread(_sin_regenerar):
            # El chequeo de enlace y el veto viven DENTRO de
            # ``_restaurar_target_vacio_sync``, en el mismo hilo que el
            # rmdir/rename — no como ``await``s separados acá (review CodeRabbit
            # #404): un ``asyncio.to_thread`` por chequeo devuelve el control al
            # loop entre medio, así que el target podía volverse un enlace EN esa
            # ventana y el fail-closed no lo vería — exactamente el escenario que
            # existe para prevenir. Fusionarlo con la mutación es la misma razón
            # por la que rmdir+rename ya iban juntos (ver el docstring del método).
            if await _fs_op_with_retry(self._restaurar_target_vacio_sync):
                logger.info(
                    "'%s' no fue regenerado por la herramienta; se conserva el contenido previo.",
                    self._target,
                )
            return
        # El sitio que motivó todo: sobre un backup enlazado, ``rmtree`` lanza
        # ``OSError`` (symlink) o borra el contenido del destino ajeno (junction de
        # Windows), y esto corre en el camino de ÉXITO — donde ``__aexit__`` traga el
        # OSError y nadie se entera.
        await _borrar_arbol_o_enlace(self._backup)

    def _restaurar_target_vacio_sync(self) -> bool:
        """Chequeos fail-closed + rmdir del target vacío + rename del backup.

        Todo en una sola unidad síncrona — no sólo el rmdir+rename. El chequeo de
        enlace y el veto de lease corren ACÁ, no como ``await``s separados en el
        caller (review CodeRabbit #404): un hop por ``asyncio.to_thread`` devuelve
        el control al event loop, así que el target podía volverse un enlace en
        esa ventana entre "lo chequeé" y "lo mutaría" — la misma clase de TOCTOU
        que el rmdir+rename fusionado ya evita para la cancelación. Fusionar
        TODO (chequeo + mutación) es la única forma de que "no hay ventana" sea
        cierto también para el fail-closed.

        Devuelve ``True`` si restauró, ``False`` si se omitió (enlace o veto).
        Idempotente ante reintentos de ``_fs_op_with_retry``: si el rename falla
        tras un rmdir ya exitoso, el segundo intento ve ``target`` inexistente y
        salta directo al rename.

        ``link_kind_or_raise`` y no ``link_kind`` (review CodeRabbit #404): esta
        función entera corre dentro de UN ``_fs_op_with_retry`` en el caller, así
        que un ``OSError`` de inspección transitorio se retryea ahí — con
        ``link_kind`` (que traga el ``OSError``) ese retry nunca se activaba y el
        bloqueo se leía como "no es un enlace".
        """
        tipo_de_enlace = link_kind_or_raise(self._target)
        if tipo_de_enlace is not None:
            logger.critical(
                "Restauración de target vacío en '%s' OMITIDA: el target es un enlace (%s), no un "
                "directorio real; rmdir/rename no pueden garantizar no tocar un árbol ajeno. "
                "El backup queda en '%s' para recuperación manual.",
                self._target,
                tipo_de_enlace,
                self._backup,
            )
            return False
        # El veto de lease también cubre ESTE restore (review Codex #401): es el
        # mismo defecto #1 del repo cometido dentro de la propia clase — se
        # cableó en la rama de excepción de __aexit__ y se olvidó acá, que es
        # justo donde vive la restauración de target vacío que este PR agregó.
        # Si el heartbeat perdió la lease durante un run que igual "termina
        # bien", restaurar sin consultar el veto pisaría lo que un dueño
        # concurrente empezó a escribir en el target vacío.
        if not self._veto_permite_restaurar():
            logger.critical(
                "Restauración de target vacío en '%s' OMITIDA: se perdió la exclusividad del recurso. "
                "El backup queda en '%s' para recuperación manual.",
                self._target,
                self._backup,
            )
            return False
        if self._target.exists():
            self._target.rmdir()
        assert self._backup is not None  # invariante: el caller ya lo verificó
        self._backup.rename(self._target)
        return True
