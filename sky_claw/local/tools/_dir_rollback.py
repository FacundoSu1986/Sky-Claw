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
import logging
import pathlib
import shutil
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("SkyClaw.DirectoryRollback")

#: Reintentos ante fallos transitorios de FS en Windows (WinError 5/32): renombrar
#: o borrar un directorio recién tocado puede fallar por handles del AV/indexer que
#: se liberan en milisegundos. Se reintenta con backoff corto antes de rendirse.
_FS_RETRIES = 5
_FS_BACKOFF_SECONDS = 0.1


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

    def __init__(self, target_dir: pathlib.Path, *, enabled: bool = True) -> None:
        self._target = target_dir
        self._enabled = enabled
        self._backup: pathlib.Path | None = None
        #: M-7: refleja si el rollback en el path de excepción se COMPLETÓ. El
        #: restore es best-effort (traga OSError sin re-raise), así que el caller no
        #: puede confiar en "hubo excepción ⇒ rolled_back=True". Este flag distingue
        #: un restore exitoso de un fallo silencioso de rmtree/rename que dejó el
        #: output parcial en disco. Espeja SnapshotTransactionLock.rollback_completed.
        self.rollback_completed: bool = False

    async def __aenter__(self) -> DirectoryRollback:
        if not self._enabled:
            return self
        if not await asyncio.to_thread(self._target.exists):
            # Primer run: no hay estado previo que preservar.
            return self
        backup = self._target.with_name(f"{self._target.name}.rollback-{time.time_ns()}")
        try:
            await _fs_op_with_retry(self._target.rename, backup)
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
                await self._restore_backup()
                # M-7: sólo aquí el rollback se considera COMPLETADO. Si
                # _restore_backup lanza, no se llega a esta línea y el flag queda
                # False, señalando al caller un rollback fallido.
                self.rollback_completed = True
            else:
                await self._discard_backup()
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
            await self._discard_backup()
        except OSError:
            logger.warning(
                "commit(): no se pudo descartar el backup de '%s' (queda huérfano)", self._target, exc_info=True
            )
        self._backup = None

    async def _restore_backup(self) -> None:
        """Descarta el parcial nuevo y restaura el directorio original."""
        if await asyncio.to_thread(self._target.exists):
            await _fs_op_with_retry(shutil.rmtree, self._target)
        if self._backup is not None and await asyncio.to_thread(self._backup.exists):
            await _fs_op_with_retry(self._backup.rename, self._target)
            logger.warning("Rollback: '%s' restaurado desde backup tras fallo del pipeline", self._target)

    async def _discard_backup(self) -> None:
        """Descarta el backup tras un run exitoso."""
        if self._backup is not None and await asyncio.to_thread(self._backup.exists):
            await _fs_op_with_retry(shutil.rmtree, self._backup)
