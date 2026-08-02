"""Ancla: un warning emitido durante el GC acusa a quien fugó, no a quien pasaba.

``pyproject.toml`` convierte ``PytestUnraisableExceptionWarning`` en error
(#409). Ese warning nace cuando el intérprete finaliza un objeto cuyo ``__del__``
protesta, y para un recurso atrapado en un **ciclo de referencias** eso no pasa
al soltarlo: pasa cuando corre el GC generacional, disparado por presión de
asignación en un test posterior y arbitrario. pytest le carga la falla a ese
test.

Sin el fixture ``_gc_al_cerrar_cada_test`` de ``tests/conftest.py``, la suite
acusa inocentes: #413 culpó a ``test_ningun_modulo_borra_arboles_con_shutil_rmtree``
—que sólo hace ``ast.parse`` en memoria— y en otra corrida a
``test_wrong_token_returns_false``; #414 reprodujo la firma sobre ``origin/main``
limpio en un tercer test distinto. Los tres pasaban al reejecutarse enfocados.

**Se afirma sobre el comportamiento, no sobre la presencia del fixture.** Un test
que sólo comprobara que el fixture existe y es autouse se quedaría verde ante un
``gc.collect()`` movido a un scope donde ya no corre dentro del teardown del
culpable — que es justo la forma en que esta propiedad se rompe en silencio. Acá
se levanta un pytest hijo, se fuga un socket en un ciclo a propósito y se lee a
quién acusa el reporte.

El fixture se **importa del conftest real** en el escenario protegido: una copia
local anclaría una réplica y dejaría pasar que alguien borre el original.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tomllib

_RAIZ = pathlib.Path(__file__).resolve().parent.parent

#: Dos tests en orden: el que fuga y el que sólo asigna memoria. El ciclo
#: (``self.yo_mismo = self``) es lo que impide que el refcount llegue a cero, así
#: que el socket sobrevive al test que lo abrió y muere cuando al GC le toca.
_MODULO_DE_PRUEBA = """
import socket


class _Ciclo:
    def __init__(self):
        self.sock = socket.socket()
        self.yo_mismo = self


def test_culpable_fuga_un_socket_en_un_ciclo():
    _Ciclo()


def test_inocente_solo_asigna_memoria():
    basura = [[] for _ in range(200_000)]
    assert len(basura) == 200_000
"""


def _ini_del_hijo() -> str:
    """Reproduce en el hijo el ``filterwarnings`` REAL de ``pyproject.toml``.

    Se lee en vez de copiarse porque el escenario depende de la lista completa y
    la dependencia no es obvia: ``error::ResourceWarning`` es lo que convierte la
    queja del ``__del__`` del socket en una excepción, y sin excepción no hay
    unraisable ni, por lo tanto, nada que atribuir. Una copia literal a la que le
    faltara esa línea dejaría los dos escenarios en verde y el ancla no probaría
    nada. ``test_project_config.py`` fija esa lista por igualdad.
    """
    with (_RAIZ / "pyproject.toml").open("rb") as archivo:
        filtros = tomllib.load(archivo)["tool"]["pytest"]["ini_options"]["filterwarnings"]
    lineas = "\n".join(f"    {filtro}" for filtro in filtros)
    return f"[pytest]\nfilterwarnings =\n{lineas}\n"


#: Reexporta el fixture REAL. ``noqa: F401`` porque pytest lo consume por
#: registro, no por referencia.
_CONFTEST_PROTEGIDO = "from tests.conftest import _gc_al_cerrar_cada_test  # noqa: F401\n"


def _correr_pytest_hijo(carpeta: pathlib.Path) -> str:
    """Levanta un pytest aislado en *carpeta* y devuelve su reporte.

    Subproceso y no ``pytester``: el escenario necesita un intérprete donde el
    estado del GC no venga contaminado por los ~3900 tests de esta misma sesión.

    El entorno se **hereda** y sólo se le agrega ``PYTHONPATH``: armar un env
    mínimo a mano rompe en Windows —que necesita ``SYSTEMROOT`` y su ``PATH``
    real para levantar el intérprete—, y el job ``windows-latest`` es el único
    bloqueante del gate.
    """
    entorno = os.environ.copy()
    entorno["PYTHONPATH"] = str(_RAIZ)
    proceso = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:randomly", "-p", "no:cacheprovider", "-v", "--no-header"],
        cwd=carpeta,
        capture_output=True,
        text=True,
        timeout=120,
        env=entorno,
        check=False,
    )
    return proceso.stdout + proceso.stderr


def _preparar(carpeta: pathlib.Path, *, con_fixture: bool) -> pathlib.Path:
    escenario = carpeta / ("protegido" if con_fixture else "desnudo")
    escenario.mkdir()
    (escenario / "test_fuga.py").write_text(_MODULO_DE_PRUEBA, encoding="utf-8")
    (escenario / "pytest.ini").write_text(_ini_del_hijo(), encoding="utf-8")
    if con_fixture:
        (escenario / "conftest.py").write_text(_CONFTEST_PROTEGIDO, encoding="utf-8")
    return escenario


def test_sin_el_fixture_el_warning_acusa_al_test_inocente(tmp_path: pathlib.Path) -> None:
    """El peligro es real: sin GC determinista, la falla cae en el que no fugó.

    Este test es el control del experimento. Si algún día pytest cambiara y
    atribuyera bien por su cuenta, ESTE test se pone rojo primero — y ahí el
    fixture pasa a ser código muerto que se puede retirar con evidencia.
    """
    reporte = _correr_pytest_hijo(_preparar(tmp_path, con_fixture=False))

    assert "test_inocente_solo_asigna_memoria FAILED" in reporte, reporte
    assert "test_culpable_fuga_un_socket_en_un_ciclo PASSED" in reporte, reporte


def test_con_el_fixture_el_warning_acusa_al_test_que_fugo(tmp_path: pathlib.Path) -> None:
    """La propiedad restaurada: la falla nombra al culpable y el inocente pasa.

    Es la mitad que importa. El inocente en verde es lo que vuelve accionable un
    rojo de CI: deja de haber corridas donde el nombre del test acusado no tiene
    relación con lo que se rompió.
    """
    reporte = _correr_pytest_hijo(_preparar(tmp_path, con_fixture=True))

    assert "test_inocente_solo_asigna_memoria PASSED" in reporte, reporte
    assert "test_culpable_fuga_un_socket_en_un_ciclo" in reporte, reporte
    assert "ERROR" in reporte or "FAILED" in reporte, reporte
