"""AsyncPathResolver — primitiva de infraestructura para resolución de rutas no bloqueante.

Mitiga el bloqueo del Event Loop provocado por llamadas síncronas a
``pathlib.Path.resolve()`` en entornos cruzados WSL2/Windows, donde la
traducción de rutas entre sistemas de ficheros puede incurrir en latencias
significativas (stat syscalls a través de 9P/Plan 9, resolución de symlinks,
hydration de OneDrive, etc.).

Invariantes de diseño:

* **Concurrencia pura**: toda I/O de disco se delega a ``asyncio.to_thread``.
* **Caché thread-safe**: diccionario interno protegido por ``asyncio.Lock``.
* **Fast-Path / Slow-Path**: lookup O(1) antes de incurrir en I/O.
* **Deduplicación de I/O**: corutinas concurrentes sobre la misma clave
  comparten el mismo ``Future`` en vuelo; solo una incurre en I/O.
* **Clave compuesta**: ``(raw_path, strict)`` para evitar que una resolución
  ``strict=False`` envenene el caché de una posterior ``strict=True``.
* **Manejo de errores quirúrgico**: sólo ``OSError`` y ``RuntimeError`` son
  traducidos a :class:`AsyncPathResolutionError`; cualquier otra excepción
  es un bug upstream y se propaga sin modificación.

La clase es *stateful* únicamente en lo que respecta al caché; no mantiene
estado de dominio, por lo que puede vivir como singleton del contenedor DI.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import Final


class AsyncPathResolutionError(RuntimeError):
    """Error de dominio para fallos de resolución asíncrona de rutas.

    Envuelve la causa raíz (``OSError`` o ``RuntimeError``) mediante la
    cadena de excepciones (``raise ... from exc``) para preservar la
    trazabilidad sin filtrar detalles del sistema de archivos al caller.
    """


class AsyncPathResolver:
    """Servicio asíncrono de resolución de rutas con caché O(1).

    Encapsula ``pathlib.Path(raw).resolve(strict=...)`` dentro de
    ``asyncio.to_thread`` para evitar el bloqueo del Event Loop y memoiza
    los resultados en un diccionario interno protegido por un
    ``asyncio.Lock`` para garantizar seguridad frente a corutinas
    concurrentes.

    La clave de caché es ``(raw_path, strict)`` para que resultados con
    distintos valores de ``strict`` se almacenen independientemente.

    La deduplicación de I/O se implementa mediante un diccionario de
    ``Future`` en vuelo: si N corutinas llegan simultáneamente con la misma
    clave, solo la primera lanza ``asyncio.to_thread``; las restantes
    aguardan el mismo ``Future`` sin incurrir en I/O adicional.
    """

    __slots__ = ("_cache", "_inflight", "_lock", "_logger")

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._cache: dict[tuple[str, bool], pathlib.Path] = {}
        self._inflight: dict[tuple[str, bool], asyncio.Future[pathlib.Path]] = {}
        self._lock: Final[asyncio.Lock] = asyncio.Lock()
        self._logger: Final[logging.Logger] = logger or logging.getLogger(
            "SkyClaw.AsyncPathResolver",
        )

    async def resolve_safe(
        self,
        raw_path: str,
        *,
        strict: bool = True,
    ) -> pathlib.Path:
        """Resuelve ``raw_path`` sin bloquear el Event Loop.

        Algoritmo:
            1. **Fast-Path**: consulta el caché bajo el lock; si hay hit,
               retorna en O(1).
            2. **Deduplicación**: si otra corutina ya está resolviendo la
               misma clave, se une a su ``Future`` sin lanzar I/O adicional.
            3. **Slow-Path**: delega ``Path(raw_path).resolve(strict=strict)``
               a ``asyncio.to_thread``.
            4. **Memoización**: guarda el resultado en el caché bajo el lock.

        Args:
            raw_path: Ruta textual sin resolver. No se normaliza previamente
                para preservar el contrato original del caller.
            strict: Si ``True`` (por defecto), la resolución falla con
                ``FileNotFoundError`` (subclase de ``OSError``) si alguno
                de los componentes del path no existe.

        Returns:
            Instancia de ``pathlib.Path`` ya resuelta.

        Raises:
            AsyncPathResolutionError: Si la resolución falla con
                ``OSError`` (incluye ``FileNotFoundError``, ``PermissionError``,
                etc.) o ``RuntimeError`` (p. ej. loops de symlinks).
        """
        key = (raw_path, strict)

        async with self._lock:
            # Fast-Path — hit de caché.
            if key in self._cache:
                return self._cache[key]
            # Deduplicación — unirse a una resolución en vuelo.
            if key in self._inflight:
                fut = self._inflight[key]
                is_owner = False
            else:
                loop = asyncio.get_running_loop()
                fut = loop.create_future()
                # Suppress "Future exception was never retrieved" when no waiters joined.
                fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
                self._inflight[key] = fut
                is_owner = True

        if not is_owner:
            return await asyncio.shield(fut)

        # Slow-Path — I/O delegada a un hilo secundario.
        try:
            resolved = await asyncio.to_thread(
                self._resolve_blocking,
                raw_path,
                strict,
            )
        except (OSError, RuntimeError) as exc:
            self._logger.error(
                "Fallo al resolver ruta %r (strict=%s): %s",
                raw_path,
                strict,
                exc,
                exc_info=True,
            )
            domain_exc = AsyncPathResolutionError(f"No se pudo resolver la ruta {raw_path!r} (strict={strict})")
            domain_exc.__cause__ = exc
            async with self._lock:
                self._inflight.pop(key, None)
            fut.set_exception(domain_exc)
            raise domain_exc from exc

        async with self._lock:
            self._cache[key] = resolved
            self._inflight.pop(key, None)
        fut.set_result(resolved)
        return resolved

    @staticmethod
    def _resolve_blocking(raw_path: str, strict: bool) -> pathlib.Path:
        """Sección crítica síncrona ejecutada en el thread pool.

        Aislada como ``staticmethod`` para evitar capturar ``self`` en el
        hilo secundario y mantener el contrato funcional puro.
        """
        return pathlib.Path(raw_path).resolve(strict=strict)
