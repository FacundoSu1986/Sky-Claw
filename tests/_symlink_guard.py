"""Guard compartido para los tests que necesitan crear enlaces de verdad.

Crear un symlink en Windows exige privilegios elevados (o modo desarrollador),
así que un test que lo intente sin ellos falla por el entorno y no por el código.
El guard sondea la capacidad **una vez** en tiempo de import y se expresa como un
marker, no como fixture: ``pytest.mark.skipif`` se evalúa al recolectar, antes de
que exista una fixture que lo pudiera decidir.

Vive acá y no en ``conftest.py`` por el mismo motivo que ``_lifecycle_guard``: es
un helper importable, y un marker no puede ser una fixture.

**Deuda declarada:** el mismo guard está copy-pasteado en 9 archivos de test con 4
variantes distintas (unas sondean con un archivo, otras con un directorio, otras
con ``os.symlink`` + una env-var). Este módulo es el destino de esa consolidación;
migra a los archivos que toca el PR que lo introduce, y los 8 restantes siguen con
su copia local hasta que alguien los mueva. Enumerarlos acá no es haberlos migrado.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

import pytest


def puede_crear_symlinks() -> bool:
    """True si este proceso puede crear un symlink a directorio.

    Se sondea con un directorio y no con un archivo porque es el caso que usan
    los tests de rollback: ``target_is_directory`` es lo que importa en Windows,
    donde el flag decide el tipo de reparse point que se crea.
    """
    try:
        with tempfile.TemporaryDirectory() as td:
            origen = pathlib.Path(td) / "origen"
            origen.mkdir()
            (pathlib.Path(td) / "enlace").symlink_to(origen, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return True


#: Skip cuando el entorno no puede crear enlaces. Sólo aplica en Windows: en POSIX
#: crear symlinks no requiere privilegios, así que un fallo del sondeo ahí es un
#: problema real y no debe silenciarse.
symlink_guard = pytest.mark.skipif(
    sys.platform == "win32" and not puede_crear_symlinks(),
    reason="Crear symlinks requiere privilegios elevados en Windows",
)

#: Los junctions son exclusivos de Windows y NO requieren privilegios, pero
#: ``os.symlink`` no los crea: hay que invocar ``mklink /J``. Los tests que
#: ejercen el modo de falla más grave (``shutil.rmtree`` atravesando un junction)
#: sólo pueden correr ahí.
junction_guard = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Los junctions (reparse points) sólo existen en Windows",
)


def crear_junction(enlace: pathlib.Path, destino: pathlib.Path) -> str | None:
    """Crea un junction ``enlace`` → ``destino``. ``None`` si pudo, o el motivo.

    Se usa ``mklink /J`` y no ``os.symlink`` porque un junction es un reparse point
    de tipo ``IO_REPARSE_TAG_MOUNT_POINT``, que es justamente el que
    ``os.path.islink()`` NO reporta como enlace — el corazón del bug que estos
    tests cubren. Sin él, un test de "junction" estaría creando un symlink y
    ejercitando el camino equivocado.

    **Devuelve el motivo en vez de un bool** a propósito: crear un junction en
    Windows **no** requiere privilegios elevados, así que un fallo acá no es "el
    entorno no soporta la feature" sino "el helper se rompió". Un ``skip``
    silencioso en la única plataforma que puede ejercer el modo de falla más grave
    hace que la ausencia de cobertura se vea exactamente igual que la cobertura
    (fue lo que pasó en la primera corrida: la suite de Windows reportó el mismo
    conteo que la de Linux). El caller convierte el motivo en un fallo con nombre.
    """
    import subprocess  # noqa: PLC0415 — sólo se necesita en Windows, no en el import del módulo

    try:
        completed = subprocess.run(  # noqa: S603 — mklink es un builtin de cmd, sin entrada del usuario
            ["cmd", "/c", "mklink", "/J", str(enlace), str(destino)],  # noqa: S607
            capture_output=True,
            check=False,
            timeout=30,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"no se pudo invocar mklink: {exc!r}"
    if completed.returncode != 0:
        return f"mklink /J salió con {completed.returncode}: stdout={completed.stdout!r} stderr={completed.stderr!r}"
    if not is_junction_real(enlace):
        return f"mklink /J dijo OK pero '{enlace}' no quedó como reparse point"
    return None


def is_junction_real(path: pathlib.Path) -> bool:
    """Confirma que *path* es un junction, sin usar el código bajo test.

    Se apoya en el ``st_reparse_tag`` del ``lstat`` (Windows) y verifica además
    que ``os.path.islink`` diga **False** — que es la propiedad que hace peligroso
    al junction y la que el test necesita tener garantizada para estar ejerciendo
    el camino correcto.
    """
    import os  # noqa: PLC0415 — local, igual que subprocess

    try:
        tiene_reparse = bool(getattr(path.lstat(), "st_reparse_tag", 0))
    except OSError:
        return False
    return tiene_reparse and not os.path.islink(path)
