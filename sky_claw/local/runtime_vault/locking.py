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

    # 1. Lock a nivel de hilos (dentro del mismo proceso)
    acquired_thread = thread_lock.acquire(blocking=(timeout > 0), timeout=timeout if timeout > 0 else -1)
    if not acquired_thread:
        raise RuntimeCloneError(
            f"El destino '{destination}' está actualmente bloqueado por otra operación en este proceso."
        )

    lock_dir = pathlib.Path(tempfile.gettempdir()) / ".skyclaw_vault_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file_path = lock_dir / f"skyclaw_clone_{key_hash[:16]}.lock"

    fd: int | None = None
    try:
        fd = os.open(os.fspath(lock_file_path), os.O_RDWR | os.O_CREAT, 0o600)

        # 2. Lock a nivel de SO (interproceso)
        if sys.platform == "win32":
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeCloneError(
                    f"El destino '{destination}' está actualmente bloqueado por otro proceso."
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeCloneError(
                    f"El destino '{destination}' está actualmente bloqueado por otro proceso."
                ) from exc

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
