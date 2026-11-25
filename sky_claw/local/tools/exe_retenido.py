"""Re-hash de un ejecutable con un handle que nadie puede reemplazar (PR 2c-2b, D7 mitad del worker).

Entre el hash que calcula el daemon y la ejecución en el worker hay una ventana (lección
F-02 de Synthesis): si el exe se reemplaza ahí, se ejecuta otro binario con el hash del
primero. :func:`retener_exe_verificado` abre el exe, lo re-hashea por ese MISMO handle, lo
compara con el hash esperado y retiene el handle mientras dura el bloque.

Propiedad: *lo que se ejecuta es lo que se hasheó*.

- **Windows:** ``CreateFileW`` con ``GENERIC_READ`` y compartición ``FILE_SHARE_READ`` sin
  ``WRITE`` ni ``DELETE``, y ``FILE_FLAG_OPEN_REPARSE_POINT`` para no seguir enlaces. Mientras
  el handle vive, nadie puede escribir, borrar ni renombrar el exe; cargarlo para ejecutarlo
  sí, porque solo pide lectura.
- **POSIX:** no hay compartición obligatoria. Se verifican hash, tipo regular, ``O_NOFOLLOW``
  y un solo enlace duro, pero un reemplazo concurrente no se impide. Sky-Claw ejecuta
  BodySlide en Windows; el camino POSIX existe para los tests y está declarado acá.

En los dos casos se rechaza un exe con más de un enlace duro: el contenido se podría
reescribir por el otro nombre sin tocar el handle retenido.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import pathlib
import re
import stat
import sys
from collections.abc import Iterator
from typing import Any, Final

_SHA256: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_BLOQUE: Final[int] = 1024 * 1024

_GENERIC_READ: Final[int] = 0x80000000
_FILE_SHARE_READ: Final[int] = 0x00000001
_OPEN_EXISTING: Final[int] = 3
_FILE_ATTRIBUTE_NORMAL: Final[int] = 0x80
_FILE_FLAG_OPEN_REPARSE_POINT: Final[int] = 0x00200000
_INVALID_HANDLE_VALUE: Final[int] = (1 << (8 * 8)) - 1


class ExeNoCoincideError(Exception):
    """El exe no es el que se hasheó, no es un archivo propio o no se pudo verificar: no se ejecuta."""


def _abrir_windows(ruta: pathlib.Path) -> int:
    """Descriptor de un handle de solo lectura que no comparte escritura ni borrado."""
    if sys.platform != "win32":  # mypy: fuera de Windows este cuerpo no se analiza
        raise ExeNoCoincideError("la apertura exclusiva por CreateFileW solo existe en Windows")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(
        str(ruta),
        _GENERIC_READ,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        codigo = ctypes.get_last_error()
        raise ExeNoCoincideError(f"no se pudo abrir '{ruta}' de forma exclusiva (Win32 {codigo})")
    try:
        descriptor: int = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise
    return descriptor


def _abrir_posix(ruta: pathlib.Path) -> int:
    return os.open(ruta, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))


def _hash_del_descriptor(descriptor: int) -> str:
    hasher = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        bloque = os.read(descriptor, _BLOQUE)
        if not bloque:
            break
        hasher.update(bloque)
    return hasher.hexdigest()


@contextlib.contextmanager
def retener_exe_verificado(ruta: pathlib.Path, sha256_esperado: str) -> Iterator[str]:
    """Abre ``ruta``, verifica su hash por el mismo handle y lo retiene durante el bloque.

    Devuelve el hash observado (igual al esperado). Ejecutá el proceso DENTRO del bloque.

    Raises:
        ExeNoCoincideError: hash mal formado o distinto, enlace, no regular, más de un
            enlace duro o no se pudo abrir. El bloque no se ejecuta.
    """
    if not isinstance(sha256_esperado, str) or _SHA256.fullmatch(sha256_esperado) is None:
        raise ExeNoCoincideError("el hash esperado del exe no es un SHA-256 en hexadecimal minúscula")
    try:
        if os.path.islink(ruta):
            raise ExeNoCoincideError(f"'{ruta}' es un enlace simbólico")
        descriptor = _abrir_windows(ruta) if sys.platform == "win32" else _abrir_posix(ruta)
    except ExeNoCoincideError:
        raise
    except OSError as exc:
        raise ExeNoCoincideError(f"no se pudo abrir '{ruta}': {exc}") from exc
    try:
        try:
            estado = os.fstat(descriptor)
            if not stat.S_ISREG(estado.st_mode):
                raise ExeNoCoincideError(f"'{ruta}' no es un archivo regular")
            if estado.st_nlink != 1:
                raise ExeNoCoincideError(f"'{ruta}' tiene {estado.st_nlink} enlaces duros")
            observado = _hash_del_descriptor(descriptor)
        except OSError as exc:
            raise ExeNoCoincideError(f"no se pudo leer '{ruta}': {exc}") from exc
        if observado != sha256_esperado:
            raise ExeNoCoincideError(
                f"el hash de '{ruta}' ({observado}) no coincide con el esperado ({sha256_esperado})"
            )
        yield observado
    finally:
        os.close(descriptor)


__all__ = ["ExeNoCoincideError", "retener_exe_verificado"]
