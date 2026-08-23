"""Mecanismo de serialización interproceso y entre hilos por destino (RV-3 B4).

Garantiza que dos operaciones de clonación hacia el mismo destino físico o
lógico se serialicen de forma determinista, evitando carreras de publicación
y verificaciones sobre destinos intermedios.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import pathlib
import sys
import tempfile
import threading
import time
from collections.abc import Generator

from sky_claw.local.runtime_vault.models import RuntimeCloneError

_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _get_thread_lock(lock_key: str) -> threading.Lock:
    """Devuelve o crea el threading.Lock asociado a la clave canónica."""
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[lock_key] = lock
        return lock


def _canonical_lock_key(path: pathlib.Path | str) -> tuple[str, str]:
    """Genera una clave canónica normalizada y un hash seguro para el lockfile."""
    p = pathlib.Path(path)
    p_abs = pathlib.Path(os.path.abspath(os.fspath(p)))

    # Resolver ancestros existentes para canonicalizar enlaces simbólicos y junctions
    actual = p_abs
    partes_inexistentes: list[str] = []
    while not actual.exists() and actual != actual.parent:
        partes_inexistentes.append(actual.name)
        actual = actual.parent
    partes_inexistentes.reverse()

    try:
        base_resuelta = actual.resolve()
    except OSError:
        base_resuelta = actual

    resultado = base_resuelta
    for parte in partes_inexistentes:
        resultado = resultado / parte

    norm = os.path.normcase(os.path.abspath(os.fspath(resultado)))
    key_hash = hashlib.sha256(norm.encode("utf-8", errors="replace")).hexdigest()
    return norm, key_hash


@contextlib.contextmanager
def destination_lock(destination: pathlib.Path | str, *, timeout: float = 0.0) -> Generator[None, None, None]:
    """Adquiere un lock interproceso e intraproceso exclusivo para el destino.

    Si otro proceso o hilo ya sostiene el lock para el mismo destino, lanza
    RuntimeCloneError de forma fail-closed (o tras expirar el timeout).
    """
    norm_path, key_hash = _canonical_lock_key(destination)
    thread_lock = _get_thread_lock(norm_path)

    start_time = time.monotonic()
    deadline = start_time + timeout if timeout > 0 else start_time

    # 1. Lock a nivel de hilos (dentro del mismo proceso)
    acquired_thread = thread_lock.acquire(blocking=(timeout > 0), timeout=timeout if timeout > 0 else -1)
    if not acquired_thread:
        raise RuntimeCloneError(
            f"El destino '{destination}' está actualmente bloqueado por otra operación en este proceso."
        )

    fd: int | None = None
    try:
        lock_dir = pathlib.Path(tempfile.gettempdir()) / ".skyclaw_vault_locks"
        try:
            lock_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeCloneError(f"No se pudo preparar el directorio de locks '{lock_dir}': {exc}") from exc

        lock_file_path = lock_dir / f"skyclaw_clone_{key_hash[:16]}.lock"

        try:
            fd = os.open(os.fspath(lock_file_path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise RuntimeCloneError(f"No se pudo abrir el lockfile '{lock_file_path}': {exc}") from exc

        # 2. Lock a nivel de SO (interproceso) con reintentos hasta el deadline si timeout > 0
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                now = time.monotonic()
                if now >= deadline:
                    msg = (
                        f"El destino '{destination}' está actualmente bloqueado por otro proceso "
                        f"(timeout de {timeout}s expirado)."
                        if timeout > 0
                        else f"El destino '{destination}' está actualmente bloqueado por otro proceso."
                    )
                    raise RuntimeCloneError(msg) from exc
                time.sleep(min(0.05, max(0.001, deadline - now)))

        yield

    finally:
        if fd is not None:
            if sys.platform == "win32":
                import msvcrt

                with contextlib.suppress(OSError):
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)
        thread_lock.release()


__all__ = [
    "destination_lock",
]
