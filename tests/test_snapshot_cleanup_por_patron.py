"""``cleanup_by_pattern`` conserva la semántica de ``rglob`` sin salir del store.

El método tiene dos garantías que tiran en direcciones opuestas y hasta ahora no
tenía ninguna prueba:

1. **La semántica del patrón es la de** ``rglob``. Reimplementarla con
   ``Path.match`` la cambiaba por dos lados: matcheaba contra la ruta absoluta
   —así que un componente de la ruta del store podía satisfacer un tramo del
   patrón por *arriba* de la base— y perdía la recursión de ``a/**/x``, que
   ``rglob`` resuelve a ``a/x``, ``a/b/x`` y más hondo. Un caller con un patrón
   válido se quedaba sin borrar nada.
2. **Lo que puede borrarse lo decide el recorrido link-aware.** ``rglob`` entra a
   un junction y devuelve rutas de adentro; el ``unlink`` se llevaba archivos que
   no son del store.

Se prueban las dos por separado, porque un cambio que rompa una puede dejar la
otra intacta — que es exactamente cómo este método llegó a tener las dos mal a
la vez.
"""

from __future__ import annotations

import pathlib

import pytest

from sky_claw.app.db.snapshot_manager import FileSnapshotManager
from tests._symlink_guard import crear_junction, is_junction_real, junction_guard, symlink_guard


def _store(tmp_path: pathlib.Path) -> FileSnapshotManager:
    raiz = tmp_path / "store"
    raiz.mkdir()
    return FileSnapshotManager(snapshot_dir=raiz)


def _archivo(raiz: pathlib.Path, relativa: str, contenido: bytes) -> pathlib.Path:
    destino = raiz / relativa
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(contenido)
    return destino


async def test_un_patron_recursivo_alcanza_todas_las_profundidades(tmp_path: pathlib.Path) -> None:
    """``a/**/x`` tiene que alcanzar ``a/x``, ``a/b/x`` y ``a/b/c/x``.

    Es el caso que ``Path.match`` perdía: sólo veía ``a/b/x``. Se ejercen tres
    profundidades y no una porque el modo de falla era justamente quedarse con
    la del medio.
    """
    manager = _store(tmp_path)
    raiz = manager._snapshot_dir
    _archivo(raiz, "a/x", b"1" * 10)
    _archivo(raiz, "a/b/x", b"2" * 20)
    _archivo(raiz, "a/b/c/x", b"3" * 30)
    ajeno_por_nombre = _archivo(raiz, "a/b/otro", b"4" * 40)

    resultado = await manager.cleanup_by_pattern("a/**/x")

    assert resultado.deleted_count == 3
    assert resultado.freed_bytes == 60
    assert not (raiz / "a/x").exists()
    assert not (raiz / "a/b/x").exists()
    assert not (raiz / "a/b/c/x").exists()
    assert ajeno_por_nombre.read_bytes() == b"4" * 40


async def test_dry_run_cuenta_sin_borrar(tmp_path: pathlib.Path) -> None:
    manager = _store(tmp_path)
    archivo = _archivo(manager._snapshot_dir, "a/b/x", b"z" * 15)

    resultado = await manager.cleanup_by_pattern("**/x", dry_run=True)

    assert (resultado.deleted_count, resultado.freed_bytes) == (1, 15)
    assert archivo.read_bytes() == b"z" * 15


@symlink_guard
async def test_un_symlink_no_deja_borrar_lo_de_afuera(tmp_path: pathlib.Path) -> None:
    """El patrón matchea, pero el archivo no es alcanzable sin atravesar el enlace."""
    ajeno = tmp_path / "ajeno"
    ajeno.mkdir()
    victima = ajeno / "x"
    victima.write_bytes(b"de otro" * 10)

    manager = _store(tmp_path)
    propio = _archivo(manager._snapshot_dir, "a/x", b"propio")
    (manager._snapshot_dir / "puente").symlink_to(ajeno, target_is_directory=True)

    resultado = await manager.cleanup_by_pattern("**/x")

    assert not propio.exists(), "lo propio sí se borra"
    assert victima.read_bytes() == b"de otro" * 10, "lo de afuera del store, no"
    assert resultado.deleted_count == 1


@junction_guard
async def test_un_junction_no_deja_borrar_lo_de_afuera(tmp_path: pathlib.Path) -> None:
    """La mitad Windows, que es donde ``rglob`` sí entra.

    ``Path.rglob`` frena en un symlink —decide con
    ``is_dir(follow_symlinks=False)``— pero no en un junction, porque
    ``IO_REPARSE_TAG_MOUNT_POINT`` no es un symlink para ``os.path.islink``. Acá
    ``rglob`` devuelve la ruta de adentro del junction y lo único que impide el
    ``unlink`` es que el recorrido link-aware no la haya enumerado.
    """
    ajeno = tmp_path / "ajeno"
    ajeno.mkdir()
    victima = ajeno / "x"
    victima.write_bytes(b"de otro" * 10)

    manager = _store(tmp_path)
    propio = _archivo(manager._snapshot_dir, "a/x", b"propio")
    assert crear_junction(manager._snapshot_dir / "puente", ajeno) is None
    assert is_junction_real(manager._snapshot_dir / "puente")

    # El recorrido crudo SÍ la alcanza: sin la intersección, esto se borraba.
    assert any(ruta.name == "x" and "puente" in ruta.parts for ruta in manager._snapshot_dir.rglob("**/x"))

    resultado = await manager.cleanup_by_pattern("**/x")

    assert not propio.exists()
    assert victima.read_bytes() == b"de otro" * 10
    assert resultado.deleted_count == 1


@symlink_guard
async def test_un_padre_reemplazado_tras_enumerar_no_saca_el_borrado_del_store(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La ventana que la intersección NO cubre: el swap DESPUÉS de enumerar.

    La intersección se hace con rutas LÉXICAS, así que protege contra un enlace
    que ya existía al recorrer, pero no contra uno puesto entre la enumeración y
    el ``unlink``: la ruta léxica sigue siendo la misma y ``ruta in propios``
    pasa igual (hallazgo de CodeRabbit en #416, que no aceptó como cerrada la
    intersección sola).

    **Se reemplaza el DIRECTORIO PADRE, no el archivo hoja.** Esa distinción es
    la que hace que el escenario pruebe algo: si se reemplazara ``a/x`` por un
    symlink, el ``unlink`` borraría el symlink y **nunca** la víctima, así que la
    aserción sobre el archivo externo pasaría con revalidación y sin ella — un
    test verde que no ejerce la pérdida de datos que dice prevenir. Con el padre
    convertido en enlace, la misma ruta léxica ``a/x`` resuelve al archivo de
    afuera y el ``unlink`` sí se lo lleva.

    Lo detecta el chequeo de identidad y no el de tipo: ``lstat("a/x")`` sigue el
    componente intermedio, así que la hoja se ve como un archivo regular — lo que
    no coincide es el inode contra el capturado al enumerar.
    """
    ajeno = tmp_path / "ajeno"
    ajeno.mkdir()
    victima = ajeno / "x"
    victima.write_bytes(b"de otro proceso")

    manager = _store(tmp_path)
    propio = _archivo(manager._snapshot_dir, "a/x", b"original")
    padre = manager._snapshot_dir / "a"

    import sky_claw.app.db.snapshot_manager as modulo

    original = modulo.link_kind_and_identity_or_raise
    intercambiado = {"hecho": False}

    def _reemplazar_el_padre_antes_de_revalidar(ruta: pathlib.Path):  # type: ignore[no-untyped-def]
        if ruta == propio and not intercambiado["hecho"]:
            intercambiado["hecho"] = True
            propio.unlink()
            padre.rmdir()
            padre.symlink_to(ajeno, target_is_directory=True)
        return original(ruta)

    monkeypatch.setattr(modulo, "link_kind_and_identity_or_raise", _reemplazar_el_padre_antes_de_revalidar)

    resultado = await manager.cleanup_by_pattern("**/x")

    assert intercambiado["hecho"], "el escenario tiene que haber ejercido el reemplazo"
    # La aserción que carga el peso: sin la revalidación esto no existe. Medido
    # neutralizándola — el `unlink` sobre la ruta léxica `a/x` atraviesa el padre
    # enlazado y se lleva el archivo de afuera del store.
    assert victima.exists(), "el archivo de AFUERA del store fue borrado: la revalidación no protegió"
    assert victima.read_bytes() == b"de otro proceso"
    assert resultado.errors, "un cambio de identidad se reporta, no se borra en silencio"
    assert resultado.deleted_count == 0
