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


@symlink_guard
async def test_un_store_apuntado_por_un_enlace_no_se_ve_vacio(tmp_path: pathlib.Path) -> None:
    """La raíz es del caller; los enlaces de adentro no.

    Los recorridos link-aware no atraviesan un enlace, así que un store
    relocalizado a otro disco por junction —práctica corriente— se veía **vacío**:
    cero bytes medidos, ninguna limpieza efectiva y estadísticas en blanco, sin
    ningún error que lo delatara (hallazgo de qodo-merge en #416, reproducido
    antes de aceptarlo).

    El arreglo va en el manager y no en la primitiva. Quien construye el manager
    **nombró** esa ruta, así que seguirla es obedecerlo; un enlace que aparece
    dentro del árbol no lo nombró nadie, y ahí seguirlo es salirse del store.
    Aflojar la primitiva habría roto la igualdad medir==borrar para una raíz
    enlazada, que es justo lo que este PR ancla.
    """
    real = tmp_path / "store_real"
    real.mkdir()
    (real / "a" / "b").mkdir(parents=True)
    (real / "a" / "b" / "x").write_bytes(b"z" * 500)

    enlace = tmp_path / "store"
    enlace.symlink_to(real, target_is_directory=True)

    manager = FileSnapshotManager(snapshot_dir=enlace)

    assert await manager._calculate_total_size() == 500, "el store apuntado por un enlace NO está vacío"

    resultado = await manager.cleanup_by_pattern("**/x")

    assert (resultado.deleted_count, resultado.freed_bytes) == (1, 500)
    assert not (real / "a" / "b" / "x").exists()


@symlink_guard
async def test_seguir_la_raiz_no_afloja_los_enlaces_de_adentro(tmp_path: pathlib.Path) -> None:
    """La otra mitad: resolver la raíz no puede volver alcanzable lo de afuera.

    Sin este test, "resolver la raíz" podría degenerar en "seguir cualquier
    enlace" y nadie se enteraría — que es exactamente la forma en que una
    excepción razonable se come la regla que la contiene.
    """
    real = tmp_path / "store_real"
    real.mkdir()
    (real / "propio").write_bytes(b"mio")

    ajeno = tmp_path / "ajeno"
    ajeno.mkdir()
    victima = ajeno / "propio"
    victima.write_bytes(b"de otro proceso")
    (real / "puente").symlink_to(ajeno, target_is_directory=True)

    enlace = tmp_path / "store"
    enlace.symlink_to(real, target_is_directory=True)

    manager = FileSnapshotManager(snapshot_dir=enlace)

    assert await manager._calculate_total_size() == 3, "sólo los bytes propios; el puente no aporta"

    resultado = await manager.cleanup_by_pattern("**/propio")

    assert resultado.deleted_count == 1
    assert victima.read_bytes() == b"de otro proceso", "lo de afuera sigue fuera de alcance"


@symlink_guard
async def test_initialize_sobrevive_a_un_enlace_roto_como_raiz(tmp_path: pathlib.Path) -> None:
    """Un enlace ROTO como store no puede tirar abajo la inicialización.

    ``Path.mkdir(exist_ok=True)`` re-lanza ``FileExistsError`` sobre un enlace
    colgante: el ``exist_ok`` sólo perdona cuando la ruta ``is_dir()``, y un
    enlace roto no lo es. Medido, ése era el comportamiento **antes** de que el
    manager resolviera su raíz::

        sin resolve() -> FileExistsError: 17
        con resolve() -> initialize() OK, y el enlace queda resuelto

    Es decir: resolver la raíz —que se agregó por el store que se veía vacío—
    además cierra este crash, porque un ``resolve()`` no estricto sigue el
    enlace y ``mkdir`` termina creando el destino que faltaba.

    Se ancla porque la relación es contraintuitiva y un futuro "esto no hace
    falta, saquemos el resolve" la rompería en silencio (revisión qodo-merge en
    #416, que atribuía el crash a este cambio cuando en verdad lo evita).
    """
    roto = tmp_path / "store"
    destino = tmp_path / "destino_todavia_inexistente"
    roto.symlink_to(destino, target_is_directory=True)
    assert roto.is_symlink() and not roto.exists(), "el escenario arranca con un enlace colgante"

    manager = FileSnapshotManager(snapshot_dir=roto)
    await manager.initialize()

    assert destino.is_dir(), "el destino que faltaba se creó"
    assert roto.exists(), "el enlace dejó de estar colgante"
