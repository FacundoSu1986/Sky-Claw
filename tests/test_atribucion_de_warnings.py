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
import re
import subprocess
import sys
import tomllib

from tests.conftest import GC_POR_TEST

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


def ini_del_hijo() -> str:
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


#: Reexporta el fixture y la opción REALES. ``noqa: F401`` porque pytest los
#: consume por registro, no por referencia. Los DOS escenarios usan este mismo
#: conftest: la única variable que cambia entre ellos es si se pasa
#: ``--gc-por-test`` en la línea de comandos, que es exactamente la decisión que
#: el ancla tiene que medir.
_CONFTEST = "from tests.conftest import _gc_al_cerrar_cada_test, pytest_addoption  # noqa: F401\n"


def correr_pytest_hijo(carpeta: pathlib.Path, *args: str) -> str:
    """Levanta un pytest aislado en *carpeta* y devuelve su reporte.

    Subproceso y no ``pytester``: el escenario necesita un intérprete donde el
    estado del GC no venga contaminado por los ~3900 tests de esta misma sesión.

    El entorno se **hereda** y a ``PYTHONPATH`` se le **antepone** la raíz en vez
    de reemplazarlo: armar un env mínimo a mano rompe en Windows —que necesita
    ``SYSTEMROOT`` y su ``PATH`` real para levantar el intérprete—, y pisar un
    ``PYTHONPATH`` preexistente rompería en cualquier entorno que resuelva sus
    dependencias por ahí (virtualenvs armados a mano, imágenes de CI, Nix), con
    un ``ModuleNotFoundError`` que se leería como un fallo de este ancla y no del
    entorno.
    """
    entorno = os.environ.copy()
    heredado = entorno.get("PYTHONPATH", "")
    entorno["PYTHONPATH"] = f"{_RAIZ}{os.pathsep}{heredado}" if heredado else str(_RAIZ)
    proceso = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "-v",
            "--no-header",
            *args,
        ],
        cwd=carpeta,
        capture_output=True,
        text=True,
        timeout=120,
        env=entorno,
        check=False,
    )
    return proceso.stdout + proceso.stderr


def _preparar(carpeta: pathlib.Path, *, con_flag: bool) -> pathlib.Path:
    escenario = carpeta / ("con_flag" if con_flag else "sin_flag")
    escenario.mkdir()
    (escenario / "test_fuga.py").write_text(_MODULO_DE_PRUEBA, encoding="utf-8")
    (escenario / "pytest.ini").write_text(ini_del_hijo(), encoding="utf-8")
    (escenario / "conftest.py").write_text(_CONFTEST, encoding="utf-8")
    return escenario


def acusados(reporte: str) -> set[str]:
    """Los tests que el reporte marca como ``FAILED`` o ``ERROR``, por nombre.

    Lee el bloque *short test summary info*, que es donde pytest escribe una
    línea por test señalado y en qué fase cayó.

    **Se extrae el conjunto y se compara por igualdad** en vez de preguntar si un
    nombre aparece: el nombre del culpable figura en el reporte aunque haya
    pasado —la línea ``... PASSED`` lo contiene— así que un ``in reporte`` se
    queda verde sin que la acusación sea la correcta. Ese era el agujero de la
    primera versión de este ancla, y es la misma clase de defecto que el ancla
    existe para atajar: afirmar menos de lo que se dice verificar.

    Un test puede pasar en la fase ``call`` y romper en ``teardown`` —que es
    justo lo que pasa acá, porque el warning se emite al recolectar— así que
    ``PASSED`` y ``ERROR`` conviven para el mismo nombre. Por eso la pregunta
    correcta es a quién señala el resumen, no si el nombre está.
    """
    return set(re.findall(r"^(?:FAILED|ERROR)\s+\S+::(\w+)", reporte, re.MULTILINE))


def test_sin_el_flag_el_warning_acusa_al_test_inocente(tmp_path: pathlib.Path) -> None:
    """El peligro es real: sin GC determinista, la falla cae en el que no fugó.

    Es el control del experimento, y también la razón de que el flag exista: éste
    es el estado por defecto de la suite, el que produjo tres acusaciones falsas
    en #413 y #414. Si algún día pytest atribuyera bien por su cuenta, ESTE test
    se pone rojo primero — y ahí el flag pasa a ser código muerto que se puede
    retirar con evidencia, en vez de quedarse "por las dudas".
    """
    reporte = correr_pytest_hijo(_preparar(tmp_path, con_flag=False))

    assert acusados(reporte) == {"test_inocente_solo_asigna_memoria"}, reporte


def test_con_el_flag_el_warning_acusa_al_test_que_fugo(tmp_path: pathlib.Path) -> None:
    """La propiedad que compra ``--gc-por-test``: la falla nombra al culpable.

    Es la mitad que importa. Por igualdad y no por presencia: el señalado tiene
    que ser el culpable **y nadie más**, que es lo que vuelve accionable un rojo
    de CI. Deja de haber corridas donde el nombre del test acusado no tiene
    relación con lo que se rompió.
    """
    reporte = correr_pytest_hijo(_preparar(tmp_path, con_flag=True), GC_POR_TEST)

    assert acusados(reporte) == {"test_culpable_fuga_un_socket_en_un_ciclo"}, reporte
