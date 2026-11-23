"""Flag de creación para lanzar procesos sin abrir una consola visible.

El ``.exe`` congelado es ``console=False``. En Windows, cuando un proceso sin consola
lanza un programa de consola sin ``CREATE_NO_WINDOW``, ese hijo recibe una consola
NUEVA: el operador ve parpadear ventanas negras, que desde afuera parecen un virus.
La consola solo queda oculta si TODOS los lanzamientos pasan el flag. Lo verifica,
por enumeración AST, ``tests/test_subprocesos_sin_consola.py``.

El flag no cambia qué se ejecuta ni sus handles: stdin/stdout/stderr siguen siendo
los que pasa el llamador (``PIPE``/``DEVNULL``). Solo evita que se cree la ventana.
"""

from __future__ import annotations

import sys

#: Windows ``CREATE_NO_WINDOW``.
CREATE_NO_WINDOW = 0x08000000


def creationflags_sin_consola(extra: int = 0) -> int:
    """Devuelve ``extra`` con ``CREATE_NO_WINDOW`` agregado en Windows.

    En POSIX devuelve SIEMPRE 0: todo ``creationflags`` es de Windows y
    ``subprocess`` rechaza cualquier valor distinto de 0 fuera de Windows, así que
    un ``extra`` (p. ej. ``CREATE_NEW_PROCESS_GROUP``) no puede romper el lanzamiento.
    """
    if sys.platform == "win32":
        return extra | CREATE_NO_WINDOW
    return 0
